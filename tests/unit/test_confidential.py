"""Unit tests for confidential-upstream mode (routstr/confidential.py and
routstr/confidential_ws.py).

The websocket tests use a real ``websockets`` server in a background thread as
a stand-in for the cu-sidecar, and the node's proxy
runs under starlette's TestClient portal loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import threading
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest
import websockets
from coincurve import PublicKeyXOnly
from fastapi import FastAPI
from starlette.testclient import TestClient

import routstr.confidential as conf
import routstr.confidential_ws as cws
from routstr.core.settings import settings
from routstr.nostr.sdk import generate_keypair

SID = "a" * 64  # sidecar's protocol sid is 32-byte hex
RATES = {"in": 1000.0, "cached_in": 500.0, "out": 2000.0}


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Module-level caches must not leak between tests."""
    monkeypatch.setattr(
        conf, "_SIDECAR_CACHE", {"at": 0.0, "offer": None, "failed_at": None}
    )
    monkeypatch.setattr(conf, "_SIGNED_OFFERS", conf.OrderedDict())
    monkeypatch.setattr(conf, "_LAST_OFFER", {"payload": None, "offer": None})
    monkeypatch.setattr(conf, "_FIXED_PRICING_LOGGED", {"done": False})
    monkeypatch.setattr(cws, "_active_sessions", 0)
    monkeypatch.setattr(cws, "_active_zk", 0)
    monkeypatch.setattr(cws, "_live_sids", {})
    monkeypatch.setattr(cws, "_pending", {})
    monkeypatch.setattr(cws, "_pending_by_addr", {})
    monkeypatch.setattr(cws, "_unused_by_ip", {})


@pytest.fixture()
def node_identity(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Install a throwaway node Nostr identity; returns (secret_hex, pub_hex)."""
    nsec, _npub = generate_keypair()
    from routstr.nostr.sdk import parse_keypair

    secret_hex, pub_hex = parse_keypair(nsec)
    monkeypatch.setattr(settings, "nsec", nsec)
    monkeypatch.setattr(settings, "npub", _npub)
    return secret_hex, pub_hex


def _verify_schnorr(payload: str, sig_hex: str, pub_hex: str) -> bool:
    msg = hashlib.sha256(payload.encode("utf-8")).digest()
    return PublicKeyXOnly(bytes.fromhex(pub_hex)).verify(bytes.fromhex(sig_hex), msg)


class MockSidecar:
    """A websockets server in its own thread/loop, scripted per test."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.port = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            server = await websockets.serve(self._handler, "127.0.0.1", 0)
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.05)
            server.close()
            await server.wait_closed()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())

    def __enter__(self) -> "MockSidecar":
        self._thread.start()
        assert self._ready.wait(timeout=10), "mock sidecar did not start"
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


def _use_sidecar(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    monkeypatch.setattr(
        settings, "confidential_sidecar_url", f"http://127.0.0.1:{port}"
    )


# ---------------------------------------------------------------------------
# offer / selection
# ---------------------------------------------------------------------------


class _Up:
    def __init__(self, base_url: str, provider_type: str = "venice") -> None:
        self.base_url = base_url
        self.provider_type = provider_type
        self.provider_fee = 1.0


async def test_offer_signed_and_verifiable(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _secret, pub_hex = node_identity
    monkeypatch.setattr(settings, "confidential_sidecar_url", "http://127.0.0.1:7443")
    monkeypatch.setattr(settings, "http_url", "https://node.example.com")
    monkeypatch.setattr(settings, "cashu_mints", ["https://mint.example"])

    async def fake_sidecar_offer() -> dict[str, Any]:
        return {
            "upstream_host": "api.venice.ai",
            "head_template": "POST / HTTP/1.1\r\n\r\n",
            "key_length": 42,
            "suffix_keys": ["model", "max_tokens", "stream", "stream_options"],
            "chat": {
                "cap_keys": ["max_tokens"],
                "fixed_pins": [
                    ["stream", True],
                    ["stream_options", {"include_usage": True}],
                ],
            },
            "max_tokens_cap": 4096,
        }

    cand = conf.Candidate(object(), _Up("https://api.venice.ai/api/v1"))
    monkeypatch.setattr(conf, "cached_sidecar_offer", fake_sidecar_offer)
    monkeypatch.setattr(conf, "offered_models", lambda side: {"m": cand})
    monkeypatch.setattr(
        conf,
        "price_entry",
        lambda mid, c: {"in": 1000.0, "cached_in": 500.0, "out": 2000.0},
    )

    offer = await conf.build_offer()
    assert offer["v"] == conf.CONFIDENTIAL_VERSION
    assert offer["ws"] == "wss://node.example.com/v1/confidential/ws"
    assert offer["notary_pubkey"] == pub_hex
    assert offer["mints"] == ["https://mint.example"]
    assert offer["price_list"] == {
        "m": {"in": 1000.0, "cached_in": 500.0, "out": 2000.0}
    }
    assert _verify_schnorr(offer["sig_payload"], offer["sig"], pub_hex)
    payload = json.loads(offer["sig_payload"])
    assert payload["price_list"] == offer["price_list"] and "sig" not in payload
    # The pinned chat suffix is the sidecar's, signed.
    assert payload["chat"] == (await fake_sidecar_offer())["chat"]
    assert payload["suffix_keys"] == ["model", "max_tokens", "stream", "stream_options"]


def test_offer_unsigned_without_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "nsec", "")
    assert conf.sign_payload({"a": 1}) is None


def test_candidate_is_the_provider_behind_the_sidecar_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pricing must use the provider the sidecar talks to, not the top-ranked one."""
    import routstr.proxy as proxy

    best = (object(), _Up("https://openrouter.ai/api/v1", "openrouter"))
    nested = (object(), _Up("https://api.venice.ai/x", "routstr"))
    venice = (object(), _Up("https://api.venice.ai/api/v1"))
    monkeypatch.setattr(proxy, "get_candidates", lambda mid: [best, nested, venice])
    cand = conf.candidate_for("m", "api.venice.ai")
    assert cand is not None and cand.upstream is venice[1]
    assert conf.candidate_for("m", "api.example.com") is None


def test_only_chat_models_are_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Image and embedding models (zero token rates) are never offered for chat."""
    from types import SimpleNamespace

    import routstr.proxy as proxy

    def model(mid: str, out: list[str]) -> Any:
        arch = SimpleNamespace(output_modalities=out)
        return SimpleNamespace(id=mid, architecture=arch)

    models = [
        model("chat", ["text"]),
        model("img", ["image"]),
        model("emb", ["embedding"]),
        SimpleNamespace(id="bare"),
    ]
    monkeypatch.setattr(proxy, "get_unique_models", lambda: models)
    venice = _Up("https://api.venice.ai/api/v1")
    monkeypatch.setattr(
        proxy, "get_candidates", lambda mid: [(SimpleNamespace(id=mid), venice)]
    )
    offered = conf.offered_models({"upstream_host": "api.venice.ai"})
    assert set(offered) == {"chat"}


class _PrefixUp(_Up):
    """A provider whose request bodies carry the model id without a prefix."""

    def transform_model_name(self, model_id: str) -> str:
        return model_id.removeprefix("vendor/")


def test_chat_is_offered_only_under_the_providers_own_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pinned suffix sends the offered id to the provider unchanged, so a
    model whose public id is not the provider's spelling is not offered."""
    from types import SimpleNamespace

    import routstr.proxy as proxy

    arch = SimpleNamespace(output_modalities=["text"])
    public = {
        # public id: the provider's own row id
        "same": "same",
        "glm-5.3": "GLM-5.3",  # lower-cased public id
        "alias": "real-model",  # operator alias (forwarded id)
        "vendor/pfx": "vendor/pfx",  # the provider strips the prefix
        "nameless": None,
    }
    models = [SimpleNamespace(id=mid, architecture=arch) for mid in public]
    monkeypatch.setattr(proxy, "get_unique_models", lambda: models)
    up = _PrefixUp("https://api.venice.ai/api/v1")
    monkeypatch.setattr(
        proxy,
        "get_candidates",
        lambda mid: [(SimpleNamespace(id=public[mid]), up)],
    )
    offered = conf.offered_models({"upstream_host": "api.venice.ai"})
    assert set(offered) == {"same"}
    assert (
        conf.provider_model_id(conf.Candidate(SimpleNamespace(id="vendor/x"), up))
        == "x"
    )
    # Without a transform hook the model's own id is what the provider gets.
    plain = conf.Candidate(SimpleNamespace(id="M"), _Up("https://api.venice.ai"))
    assert conf.provider_model_id(plain) == "M"


def test_model_allowlist_semantics() -> None:
    allow = conf.model_allowlist
    assert allow({}) == set() and allow({"model_allowlist": []}) == set()
    assert allow({"model_allowlist": ["m"]}) == {"m"}
    # legacy name and shape
    assert allow({"text_only_ok": {"m": True, "x": False}}) == {"m"}
    # the new name wins over the old one
    assert allow({"model_allowlist": ["a"], "text_only_ok": {"m": True}}) == {"a"}
    assert conf.model_allowed("m", set()) is True
    assert conf.model_allowed("m", {"m"}) is True
    assert conf.model_allowed("m", {"other"}) is False


async def test_models_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sidecar_offer() -> dict[str, Any]:
        return {"upstream_host": "api.venice.ai"}

    monkeypatch.setattr(settings, "http_url", "https://node.example.com")
    monkeypatch.setattr(conf, "cached_sidecar_offer", fake_sidecar_offer)
    monkeypatch.setattr(conf, "offered_models", lambda side: {"m": object()})
    monkeypatch.setattr(conf, "price_entry", lambda mid, c: dict(RATES))
    fields = await conf.models_fields()
    assert fields == {
        "m": {"v": 2, "offer_url": "https://node.example.com/v1/confidential/offer"}
    }


def test_unbilled_error_statuses() -> None:
    assert {400, 404, 429, 503} <= conf.UNBILLED_ERROR_STATUSES
    assert not {500, 502, 504} & conf.UNBILLED_ERROR_STATUSES


# ---------------------------------------------------------------------------
# /ws session: billing goes through the node's reservation path
# ---------------------------------------------------------------------------


class _Snapshot:
    release_id = "rel-123"


class _Billing:
    def __init__(self) -> None:
        self.key_hash = "k"
        self.model_id = "test-model"
        self.reserved_msats = 9000
        self.snapshot = _Snapshot()
        self.rates = dict(RATES)


async def _fake_sidecar_offer() -> dict[str, Any]:
    return {"upstream_host": "api.venice.ai", "max_tokens_cap": 4096}


class _Ledger:
    """Records which of reserve/settle/release/charge the proxy called."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reserve_error: Any = None,
        settle_error: Any = None,
        settle_failures: int | None = None,
    ) -> None:
        """``settle_failures``: raise ``settle_error`` only on that many first calls."""
        self.calls: list[str] = []
        self.auth: str | None = None
        self.offer_sig: str | None = None

        async def reserve(
            auth: str,
            model: str,
            max_tokens: int,
            offer_sig: str | None = None,
            accepted_fee_msat: int | None = None,
            accepted_abandon_fee_msat: int | None = None,
        ) -> Any:
            self.auth = auth
            self.offer_sig = offer_sig
            self.accepted_fee = accepted_fee_msat
            self.calls.append("reserve")
            if reserve_error is not None:
                raise reserve_error
            return _Billing()

        async def settle(billing: Any, usage: dict[str, Any]) -> dict[str, Any]:
            self.calls.append("settle")
            failed = self.calls.count("settle") - 1
            if settle_error is not None and (
                settle_failures is None or failed < settle_failures
            ):
                raise settle_error
            return {"cost_msats": 12, "balance_msats": 88000}

        async def release(billing: Any) -> int:
            self.calls.append("release")
            return 97000

        async def charge(billing: Any) -> int:
            self.calls.append("charge")
            return 91000

        monkeypatch.setattr(cws, "reserve", reserve)
        monkeypatch.setattr(cws, "settle", settle)
        monkeypatch.setattr(cws, "release", release)
        monkeypatch.setattr(cws, "charge_reservation", charge)
        monkeypatch.setattr(cws, "cached_sidecar_offer", _fake_sidecar_offer)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(cws.confidential_ws_router)
    return app


def _sidecar_script(holder: dict[str, Any], frames: list[dict[str, Any]]) -> Any:
    """Scripted sidecar: on setup, record it and send ``frames``."""

    async def handler(ws: Any) -> None:
        async for frame in ws:
            if isinstance(frame, bytes):
                continue
            msg = json.loads(frame)
            if msg.get("type") == "setup":
                holder["setup"] = msg
                for out in frames:
                    await ws.send(json.dumps(out))
                return

    return handler


_READY = {
    "type": "ready",
    "sid": SID,
    "pubkey": "cc" * 32,
    "head_len": 228,
    "offer": {"notary_pubkey": "ee" * 32},
}
_ATTEST = {"type": "attestation", "a": {"sid": SID}, "sig": "dd" * 64}
_USAGE = {
    "type": "usage",
    "sid": SID,
    "usage": {
        "model": "test-model",
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    },
}
_SETUP = {
    "type": "setup",
    "nonce_c": "aa" * 32,
    "model": "test-model",
    "max_tokens": 12,
    "len": 150,
    "auth": "sk-client-key",
    "offer_sig": "ab" * 64,
}


def _run_session(
    monkeypatch: pytest.MonkeyPatch,
    frames: list[dict[str, Any]],
    expect: int,
    setup: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    holder: dict[str, Any] = {}
    got: list[dict[str, Any]] = []
    with MockSidecar(_sidecar_script(holder, frames)) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        with client.websocket_connect(
            f"/v1/confidential/ws?session_id={uuid.uuid4()}&v=2"
        ) as ws:
            ws.send_text(json.dumps(setup if setup is not None else _SETUP))
            for _ in range(expect):
                got.append(json.loads(ws.receive_text()))
    return holder, got


def test_session_settles_on_the_reservation(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _secret, pub_hex = node_identity
    ledger = _Ledger(monkeypatch)
    holder, got = _run_session(monkeypatch, [_READY, _ATTEST, _USAGE], 3)

    # The bearer reached the reservation, never the sidecar; the sidecar's sid
    # is bound to the (non-secret) reservation id instead.
    assert ledger.auth == "sk-client-key"
    assert "auth" not in holder["setup"]
    assert holder["setup"]["escrow"] == "rel-123"

    ready, att, receipt_msg = got
    assert ready["pubkey"] == pub_hex
    assert "offer" not in ready  # one authoritative (node-signed) offer
    assert _verify_schnorr(att["a_json"], att["sig"], pub_hex)
    receipt = receipt_msg["receipt"]
    assert receipt["cost_msats"] == 12 and receipt["balance_msats"] == 88000
    assert receipt["reserved_msats"] == 9000
    assert receipt["rates"] == RATES
    assert receipt["sid"] == SID
    assert (
        receipt["attestation_hash"]
        == hashlib.sha256(att["a_json"].encode("utf-8")).hexdigest()
    )
    assert _verify_schnorr(receipt_msg["receipt_json"], receipt_msg["sig"], pub_hex)
    assert ledger.calls == ["reserve", "settle"]


def test_attestation_of_another_session_is_not_signed(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    foreign = {**_ATTEST, "a": {"sid": "ef" * 32}}
    _, got = _run_session(monkeypatch, [_READY, foreign, _USAGE], 2)
    ready, error = got
    # Without an attestation nothing is charged and no receipt is signed.
    assert error["type"] == "error" and error["status"] == 502
    assert error["detail"]["error"]["outcome"] == "released"
    assert ledger.calls == ["reserve", "release"]


def test_usage_without_an_attestation_is_not_charged(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    _, got = _run_session(monkeypatch, [_READY, {"type": "body_released"}, _USAGE], 3)
    assert [m["type"] for m in got] == ["ready", "body_released", "error"]
    assert ledger.calls == ["reserve", "release"]


def test_abort_before_release_releases_the_reservation(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    _run_session(monkeypatch, [_READY], 1)
    assert ledger.calls == ["reserve", "release"]


def test_abort_after_release_charges_the_reservation(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """The upstream had the request but the client disclosed no usage."""
    ledger = _Ledger(monkeypatch)
    _run_session(monkeypatch, [_READY, {"type": "body_released"}], 2)
    assert ledger.calls == ["reserve", "charge"]


@pytest.mark.parametrize(
    "status,expected", [(400, "release"), (503, "release"), (502, "charge")]
)
def test_upstream_error_settlement(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    status: int,
    expected: str,
) -> None:
    ledger = _Ledger(monkeypatch)
    usage = {
        "type": "usage",
        "sid": SID,
        "usage": {"error_status": status, "choices": []},
    }
    _, got = _run_session(
        monkeypatch, [_READY, {"type": "body_released"}, _ATTEST, usage], 4
    )
    assert ledger.calls == ["reserve", expected]
    receipt = got[-1]["receipt"]
    assert got[-1]["type"] == "receipt"
    assert receipt["cost_msats"] == (0 if expected == "release" else 9000)
    assert receipt["balance_msats"] == (97000 if expected == "release" else 91000)


@pytest.mark.parametrize(
    "frame",
    [
        # Older sidecars: the top-level model is the provider's spelling.
        {"model": "Test-Model-2026", "usage": {"model": "Test-Model-2026"}},
        # Newer sidecars: the pinned model, plus what the provider said.
        {
            "model": "test-model",
            "reported_model": "Test-Model-2026",
            "usage": {"model": "Test-Model-2026"},
        },
    ],
)
def test_usage_naming_another_model_settles_at_the_pinned_rates(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    frame: dict[str, Any],
) -> None:
    """π_C2S proved the request names the pinned model: a provider that echoes
    another spelling (an alias, a canonical id) does not cost the client the
    reservation. The usage is settled at the frozen rates and logged."""
    ledger = _Ledger(monkeypatch)
    settled: list[Any] = []

    async def settle(billing: Any, usage: dict[str, Any]) -> dict[str, Any]:
        ledger.calls.append("settle")
        settled.append(billing)
        return {"cost_msats": 12, "balance_msats": 88000}

    monkeypatch.setattr(cws, "settle", settle)
    seen = _capture_warnings(monkeypatch, cws.logger)
    usage = {"type": "usage", "sid": SID, **frame}
    usage["usage"] = {
        **frame["usage"],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }
    _, got = _run_session(
        monkeypatch, [_READY, {"type": "body_released"}, _ATTEST, usage], 4
    )
    assert ledger.calls == ["reserve", "settle"]
    assert settled[0].rates == RATES
    receipt = got[-1]["receipt"]
    assert receipt["model"] == "test-model" and receipt["cost_msats"] == 12
    assert [e["reported_model"] for m, e in seen if "pinned" in m] == [
        "Test-Model-2026"
    ]


def _capture_warnings(
    monkeypatch: pytest.MonkeyPatch, logger: Any
) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []

    def warning(msg: str, *args: Any, extra: Any = None, **kw: Any) -> None:
        seen.append((msg % args if args else msg, extra))

    monkeypatch.setattr(logger, "warning", warning)
    return seen


async def test_matching_usage_model_settles_without_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def settle(billing: Any, usage: dict[str, Any]) -> dict[str, Any]:
        return {"cost_msats": 3, "balance_msats": 1}

    monkeypatch.setattr(cws, "settle", settle)
    seen = _capture_warnings(monkeypatch, cws.logger)
    out = await cws._apply_settlement(
        _Billing(), {"model": "test-model", "usage": {}}, "test-model"
    )
    assert out == (3, 1) and seen == []


def test_oversized_setup_frame_is_refused_before_parsing(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    big = {**_SETUP, "auth": "x" * (cws.MAX_SETUP_TEXT + 1)}
    _, got = _run_session(monkeypatch, [], 1, big)
    assert got[0]["type"] == "error" and got[0]["status"] == 413
    assert ledger.calls == []
    # Multi-byte text is measured in bytes.
    wide = {**_SETUP, "auth": "\u00e9" * (cws.MAX_SETUP_TEXT // 2 + 1)}
    _, got = _run_session(monkeypatch, [], 1, wide)
    assert got[0]["status"] == 413 and ledger.calls == []


def test_setup_billing_error_is_reported_with_status(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch, conf.BillingError("Insufficient balance", 402))
    _, got = _run_session(monkeypatch, [], 1)
    assert got[0] == {
        "type": "error",
        "status": 402,
        "reason": "Insufficient balance",
        "detail": "Insufficient balance",
    }
    assert ledger.calls == ["reserve"]


def test_ws_proxy_rejects_bad_version(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    holder: dict[str, int] = {}
    with MockSidecar(_sidecar_script(holder, [])) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        with client.websocket_connect(
            f"/v1/confidential/ws?session_id={uuid.uuid4()}&v=99"
        ) as ws:
            # accept-then-close: the first receive is the close frame
            msg = ws.receive()
            assert msg["type"] == "websocket.close"
            assert msg["code"] == 1008


def test_ws_proxy_refuses_v1_clients(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Protocol v1 is retired: a v1 client is refused before any setup."""

    async def v2_offer() -> dict[str, Any]:
        return {"upstream_host": "api.venice.ai", "max_tokens_cap": 4096, "v": 2}

    holder: dict[str, int] = {}
    with MockSidecar(_sidecar_script(holder, [])) as mock:
        _use_sidecar(monkeypatch, mock.port)
        monkeypatch.setattr(cws, "cached_sidecar_offer", v2_offer)
        client = TestClient(_app())
        with client.websocket_connect(
            f"/v1/confidential/ws?session_id={uuid.uuid4()}&v=1"
        ) as ws:
            msg = ws.receive()
            assert msg["type"] == "websocket.close"
            assert msg["code"] == 1008
            assert "only v2" in msg.get("reason", "")


def test_sidecar_protocol_version() -> None:
    # A sidecar that names no version predates versioning: protocol v1.
    assert conf.sidecar_protocol_version({}) == 1
    assert conf.sidecar_protocol_version({"v": 2}) == 2
    assert conf.sidecar_protocol_version({"v": "2"}) == 2
    assert conf.sidecar_protocol_version({"v": 9}) == 9
    assert conf.sidecar_protocol_version({"v": "x"}) == 0


@pytest.mark.parametrize("version", [None, 1, 3, "x"])
async def test_sidecar_without_protocol_v2_is_refused(
    monkeypatch: pytest.MonkeyPatch, version: Any
) -> None:
    """Protocol v1 is retired: a sidecar that does not speak v2 gets no offer."""
    offer: dict[str, Any] = {"upstream_host": "api.venice.ai", "head_template": "x"}
    if version is not None:
        offer["v"] = version

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=offer)

    real_client = httpx.AsyncClient

    def mocked_client(**kw: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(answer), **kw)

    monkeypatch.setattr(conf.httpx, "AsyncClient", mocked_client)
    monkeypatch.setattr(settings, "confidential_sidecar_url", "http://127.0.0.1:7444")
    with pytest.raises(conf.OfferError, match="only v2"):
        await conf.fetch_sidecar_offer()
    offer["v"] = 2
    offer["node_settles"] = True
    assert await conf.fetch_sidecar_offer() == offer


def test_ws_proxy_rejects_non_uuid_session(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    holder: dict[str, int] = {}
    with MockSidecar(_sidecar_script(holder, [])) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        with client.websocket_connect(
            "/v1/confidential/ws?session_id=not-a-uuid&v=2"
        ) as ws:
            msg = ws.receive()
            assert msg["type"] == "websocket.close"
            assert msg["code"] == 1008


# ---------------------------------------------------------------------------
# setup gate: validation, no sidecar dial before setup, limits, error frames
# ---------------------------------------------------------------------------


class _ConnectRecorder:
    """Stands in for ``websockets.connect``; records dials and refuses them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.urls: list[str] = []

        async def connect(url: str, *args: Any, **kwargs: Any) -> Any:
            self.urls.append(url)
            raise OSError("refused")

        monkeypatch.setattr(cws.websockets, "connect", connect)


def _connect(client: TestClient) -> Any:
    return client.websocket_connect(
        f"/v1/confidential/ws?session_id={uuid.uuid4()}&v=2"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_tokens", 0),
        ("max_tokens", -1),
        ("max_tokens", "12"),
        ("max_tokens", 12.5),
        ("max_tokens", True),
        ("max_tokens", 4097),  # cap + 1
        ("max_tokens", None),
        ("len", 0),
        ("len", (8 << 20) + 1),
        ("len", 1 << 40),
        ("len", "150"),
        ("model", ""),
        ("model", 7),
        ("nonce_c", "zz" * 32),
        ("nonce_c", "aa"),
        ("nonce_c", "aa" * 32 + "\n"),
        ("offer_sig", 5),
        ("offer_sig", "not-hex"),
        ("offer_sig", None),
        ("offer_sig", ""),
        ("offer_sig", "ab" * 64 + "\n"),
    ],
)
def test_setup_validation_rejects_before_reserving(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    field: str,
    value: Any,
) -> None:
    ledger = _Ledger(monkeypatch)
    dials = _ConnectRecorder(monkeypatch)
    bad = {**_SETUP, field: value}
    with _connect(TestClient(_app())) as ws:
        ws.send_text(json.dumps(bad))
        err = json.loads(ws.receive_text())
    assert err["type"] == "error" and err["status"] == 400
    assert field in err["reason"]
    assert ledger.calls == [] and dials.urls == []


def test_setup_len_bounds_accept_edges(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    for length in (1, 8 << 20):
        ledger = _Ledger(monkeypatch)
        holder, _ = _run_session(
            monkeypatch, [_READY], 1, {**_SETUP, "len": length, "max_tokens": 4096}
        )
        assert holder["setup"]["len"] == length
        assert ledger.calls == ["reserve", "release"]


def test_no_sidecar_dial_before_setup(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    dials = _ConnectRecorder(monkeypatch)
    monkeypatch.setattr(cws, "SETUP_TIMEOUT_S", 0.2)
    with _connect(TestClient(_app())) as ws:
        err = json.loads(ws.receive_text())
        assert err["status"] == 408
        assert ws.receive()["type"] == "websocket.close"
    assert dials.urls == [] and ledger.calls == []


def test_sidecar_is_dialed_only_after_the_reservation(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Sidecar unreachable after reserving: 502 and the reservation is released."""
    ledger = _Ledger(monkeypatch)
    dials = _ConnectRecorder(monkeypatch)
    _use_sidecar(monkeypatch, 7444)
    with _connect(TestClient(_app())) as ws:
        ws.send_text(json.dumps(_SETUP))
        err = json.loads(ws.receive_text())
    assert err["status"] == 502
    assert len(dials.urls) == 1 and dials.urls[0].startswith("ws://")
    assert ledger.calls == ["reserve", "release"]


def _holding_sidecar(holder: dict[str, Any]) -> Any:
    """Sidecar that accepts the setup (``ready``) and keeps the session open."""

    async def handler(ws: Any) -> None:
        async for frame in ws:
            if isinstance(frame, str) and json.loads(frame).get("type") == "setup":
                holder.setdefault("setups", []).append(json.loads(frame))
                await ws.send(json.dumps(_READY))

    return handler


def test_session_cap_refuses_after_the_reservation_and_releases_it(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A full node refuses a reserved setup with 503 before dialing the
    sidecar and releases the reservation in full (no abandon fee)."""
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(_Billing, "abandon_fee_msat", 500, raising=False)

    async def charge_fee(billing: Any, fee: int) -> int:
        ledger.calls.append(f"fee:{fee}")
        return 96500

    monkeypatch.setattr(cws, "charge_fee", charge_fee)
    monkeypatch.setattr(cws, "MAX_SESSIONS", 1)
    holder: dict[str, Any] = {}
    with MockSidecar(_holding_sidecar(holder)) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        with _connect(client) as first:
            # An idle connection takes no slot: the first setup gets it.
            with _connect(client) as idle:
                first.send_text(json.dumps(_SETUP))
                assert json.loads(first.receive_text())["type"] == "ready"
                assert cws._active_sessions == 1
                with _connect(client) as second:
                    second.send_text(json.dumps(_SETUP))
                    err = json.loads(second.receive_text())
                    assert err["status"] == 503
                    assert err["reason"] == "too many confidential sessions"
                assert idle is not None
            assert ledger.calls == ["reserve", "reserve", "release"]
            assert len(holder["setups"]) == 1  # the refused one never reached it
        # The accepted session ends unused: it keeps its abandon fee.
    assert ledger.calls == ["reserve", "reserve", "release", "fee:500"]
    assert cws._active_sessions == 0 and cws._pending == {}


def test_idle_pre_setup_sockets_cannot_block_a_credentialed_setup(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Idle sockets beyond the address's pending share are evicted oldest
    first, and none of them takes a session slot."""
    from contextlib import ExitStack

    ledger = _Ledger(monkeypatch)
    dials = _ConnectRecorder(monkeypatch)
    _use_sidecar(monkeypatch, 7444)
    monkeypatch.setattr(cws, "MAX_SESSIONS", 2)
    client = TestClient(_app())
    with ExitStack() as stack:
        idle = [
            stack.enter_context(_connect(client))
            for _ in range(cws.MAX_PENDING_PER_ADDR + 2)
        ]
        assert cws._active_sessions == 0
        with _connect(client) as ws:
            ws.send_text(json.dumps(_SETUP))
            assert json.loads(ws.receive_text())["status"] == 502  # admitted
        # The oldest idle ones were closed to make room.
        err = json.loads(idle[0].receive_text())
        assert err["status"] == 503 and "pending" in err["reason"]
        assert len(cws._pending_by_addr["testclient"]) <= cws.MAX_PENDING_PER_ADDR
    assert ledger.calls == ["reserve", "release"] and len(dials.urls) == 1
    assert cws._active_sessions == 0 and cws._pending == {}


def test_idle_sockets_from_many_addresses_cannot_block_a_setup(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A full pending pool evicts its oldest waiting connection."""
    from contextlib import ExitStack

    ledger = _Ledger(monkeypatch)
    _ConnectRecorder(monkeypatch)
    _use_sidecar(monkeypatch, 7444)
    monkeypatch.setattr(cws, "MAX_SESSIONS", 1)
    monkeypatch.setattr(cws, "MAX_PENDING", 3)
    addresses = iter(f"198.51.100.{i}" for i in range(1, 100))
    monkeypatch.setattr(cws, "client_address", lambda ws: next(addresses))
    client = TestClient(_app())
    with ExitStack() as stack:
        for _ in range(5):
            stack.enter_context(_connect(client))
        with _connect(client) as ws:
            ws.send_text(json.dumps(_SETUP))
            assert json.loads(ws.receive_text())["status"] == 502  # admitted
        assert len(cws._pending) <= 3
    assert ledger.calls == ["reserve", "release"]


def test_pending_pool_evicts_the_oldest_waiting_connection() -> None:
    states = [cws.SessionState() for _ in range(4)]
    cws._pending.clear()
    cws._pending_by_addr.clear()
    try:
        old = (cws.MAX_PENDING, cws.MAX_PENDING_PER_ADDR)
        cws.MAX_PENDING, cws.MAX_PENDING_PER_ADDR = 3, 2
        assert cws._admit_pending(states[0], "a") is None
        assert cws._admit_pending(states[1], "a") is None
        # The address's share is full: its oldest is evicted.
        assert cws._admit_pending(states[2], "a") is None
        assert states[0].evicted.is_set() and states[0] not in cws._pending
        # A connection whose setup is being reserved is never evicted.
        states[1].awaiting_setup = False
        assert cws._admit_pending(states[3], "b") is None
        assert cws._pending.keys() == {states[1], states[2], states[3]}
        late = cws.SessionState()
        assert cws._admit_pending(late, "c") is None  # the pool evicts states[2]
        assert states[2].evicted.is_set() and not states[1].evicted.is_set()
        states[3].awaiting_setup = late.awaiting_setup = False
        refused = cws._admit_pending(cws.SessionState(), "d")
        assert refused is not None and refused[0] == 503
        busy = cws.SessionState()
        cws._pending_by_addr["a"][busy] = None
        cws._pending[busy] = "a"
        busy.awaiting_setup = False
        refused = cws._admit_pending(cws.SessionState(), "a")
        assert refused is not None and refused[0] == 429
        for st in list(cws._pending):
            cws._release_pending(st)
        assert cws._pending == {} and cws._pending_by_addr == {}
    finally:
        cws.MAX_PENDING, cws.MAX_PENDING_PER_ADDR = old
        cws._pending.clear()
        cws._pending_by_addr.clear()


@pytest.mark.parametrize(
    "addr,bucket",
    [
        ("198.51.100.7", "198.51.100.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff::9", "2001:db8:1:2::/64"),
        ("::ffff:198.51.100.7", "198.51.100.7"),
        ("testclient", "testclient"),
    ],
)
def test_address_bucket(addr: str, bucket: str) -> None:
    assert cws._address_bucket(addr) == bucket


def test_offer_fetch_failure_is_an_error_frame(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)

    async def broken() -> dict[str, Any]:
        raise conf.OfferError("down")

    monkeypatch.setattr(cws, "cached_sidecar_offer", broken)
    with _connect(TestClient(_app())) as ws:
        ws.send_text(json.dumps(_SETUP))
        err = json.loads(ws.receive_text())
    assert err["type"] == "error" and err["status"] == 503
    assert ledger.calls == []


def test_offer_expired_is_a_409_error_frame(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch, conf._offer_expired("test-model"))
    _, got = _run_session(monkeypatch, [], 1, {**_SETUP, "offer_sig": "ab" * 64})
    assert got[0]["status"] == 409
    assert got[0]["detail"]["error"]["type"] == "offer_expired"


def test_offer_sig_reaches_billing_not_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    holder, _ = _run_session(
        monkeypatch, [_READY], 1, {**_SETUP, "offer_sig": "ab" * 64}
    )
    assert ledger.offer_sig == "ab" * 64
    assert "offer_sig" not in holder["setup"] and "auth" not in holder["setup"]


def test_settle_failure_finalizes_once_and_reports(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch, settle_error=RuntimeError("db down"))
    monkeypatch.setattr(cws, "SETTLE_RETRY_DELAYS", (0, 0, 0))
    _, got = _run_session(
        monkeypatch, [_READY, {"type": "body_released"}, _ATTEST, _USAGE], 4
    )
    assert got[-1]["type"] == "error" and got[-1]["status"] == 500
    assert got[-1]["detail"]["error"]["outcome"] == "charged_reservation"
    # Retried, then the fallback charged the reservation; the abort path did
    # not finalize again.
    assert ledger.calls == ["reserve"] + ["settle"] * 4 + ["charge"]


def test_a_transient_settle_failure_is_retried(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A database hiccup during settlement charges the verified usage on a
    retry, not the whole reservation."""
    ledger = _Ledger(
        monkeypatch, settle_error=RuntimeError("database is locked"), settle_failures=1
    )
    monkeypatch.setattr(cws, "SETTLE_RETRY_DELAYS", (0, 0, 0))
    _, got = _run_session(
        monkeypatch, [_READY, {"type": "body_released"}, _ATTEST, _USAGE], 4
    )
    assert got[-1]["type"] == "receipt"
    assert got[-1]["receipt"]["cost_msats"] == 12
    assert ledger.calls == ["reserve", "settle", "settle"]


def test_usage_for_a_foreign_sid_is_ignored(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    foreign = {**_USAGE, "sid": "b" * 64}
    _, got = _run_session(
        monkeypatch, [_READY, {"type": "body_released"}, _ATTEST, foreign], 3
    )
    assert [m["type"] for m in got] == ["ready", "body_released", "attestation"]
    assert ledger.calls == ["reserve", "charge"]  # no disclosure for this session


def test_session_time_limit(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """No disclosure within the post-release window: the reservation is charged."""
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(cws, "SESSION_TIMEOUT_S", 0.5)
    monkeypatch.setattr(cws, "POST_RELEASE_TIMEOUT_S", 0.5)

    async def stalling(ws: Any) -> None:
        async for frame in ws:
            if isinstance(frame, str) and json.loads(frame).get("type") == "setup":
                await ws.send(json.dumps(_READY))
                await ws.send(json.dumps({"type": "body_released"}))
                await asyncio.sleep(2)
                return

    with MockSidecar(stalling) as mock:
        _use_sidecar(monkeypatch, mock.port)
        with _connect(TestClient(_app())) as ws:
            ws.send_text(json.dumps(_SETUP))
            got = [json.loads(ws.receive_text()) for _ in range(3)]
    assert got[-1]["type"] == "error" and got[-1]["status"] == 408
    assert ledger.calls == ["reserve", "charge"]


def test_released_session_outlives_the_setup_phase_limit(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A usage disclosed after the setup-phase limit, within the post-release
    window, is settled rather than the reservation charged."""
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(cws, "SESSION_TIMEOUT_S", 0.5)
    monkeypatch.setattr(cws, "POST_RELEASE_TIMEOUT_S", 10.0)

    async def slow(ws: Any) -> None:
        async for frame in ws:
            if isinstance(frame, str) and json.loads(frame).get("type") == "setup":
                await ws.send(json.dumps(_READY))
                await ws.send(json.dumps({"type": "body_released"}))
                await asyncio.sleep(1.5)  # past the setup-phase limit
                await ws.send(json.dumps(_ATTEST))
                await ws.send(json.dumps(_USAGE))
                return

    with MockSidecar(slow) as mock:
        _use_sidecar(monkeypatch, mock.port)
        with _connect(TestClient(_app())) as ws:
            ws.send_text(json.dumps(_SETUP))
            got = [json.loads(ws.receive_text()) for _ in range(4)]
    assert [m["type"] for m in got] == [
        "ready",
        "body_released",
        "attestation",
        "receipt",
    ]
    assert ledger.calls == ["reserve", "settle"]


def test_unreleased_session_keeps_the_setup_phase_limit(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(cws, "SESSION_TIMEOUT_S", 0.5)

    async def armed(ws: Any) -> None:
        async for frame in ws:
            if isinstance(frame, str) and json.loads(frame).get("type") == "setup":
                await ws.send(json.dumps(_READY))
                await asyncio.sleep(2)
                return

    with MockSidecar(armed) as mock:
        _use_sidecar(monkeypatch, mock.port)
        with _connect(TestClient(_app())) as ws:
            ws.send_text(json.dumps(_SETUP))
            got = [json.loads(ws.receive_text()) for _ in range(2)]
    assert got[-1]["type"] == "error" and got[-1]["status"] == 408
    assert ledger.calls == ["reserve", "release"]


class _QueueSidecar:
    """A sidecar connection whose close lets one in-flight frame through, as
    the real sidecar flushes ``body_released`` written just before it saw the
    node's close."""

    def __init__(self, frames: list[dict[str, Any]], on_close: list[dict]) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        for f in frames:
            self.queue.put_nowait(json.dumps(f))
        self.on_close = on_close
        self.closed = False

    async def send(self, data: Any) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.closed:
            self.closed = True
            await asyncio.sleep(0.05)
            for f in self.on_close:
                self.queue.put_nowait(json.dumps(f))
            self.queue.put_nowait(None)

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> str:
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class _IdleClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass


@pytest.mark.parametrize(
    "late,expected",
    [([{"type": "body_released"}], "charge"), ([], "release")],
)
async def test_time_limit_hears_the_sidecars_last_frames(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    late: list[dict[str, Any]],
    expected: str,
) -> None:
    """At the time limit the node closes the sidecar session and drains its
    frames before deciding: a ``body_released`` in flight is honoured (the
    provider has the request), so the reservation is not released."""
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(cws, "SESSION_TIMEOUT_S", 0.3)
    sidecar = _QueueSidecar([_READY], late)
    state = cws.SessionState()

    async def open_session(ws: Any, sid: str, st: Any) -> Any:
        st.billing = _Billing()
        return sidecar

    monkeypatch.setattr(cws, "_open_session", open_session)
    client = _IdleClient()
    await cws._run_session(client, "s", state)  # type: ignore[arg-type]
    assert sidecar.closed
    assert any(m.get("status") == 408 for m in client.sent)
    assert state.body_released is bool(late)
    assert ledger.calls == [expected]


async def test_post_release_window_stays_inside_the_lease() -> None:
    from routstr.core.lifecycle import RequestLifetime, request_lifetime

    loop = asyncio.get_running_loop()
    now = loop.time()
    st = cws.SessionState()
    assert cws._session_deadline(st, now) == now + cws.SESSION_TIMEOUT_S
    st.released_at = now + 500
    token = request_lifetime.set(RequestLifetime(deadline=now + 2000))
    try:
        assert cws._session_deadline(st, now) == now + 500 + cws.POST_RELEASE_TIMEOUT_S
        request_lifetime.set(RequestLifetime(deadline=now + 900))
        assert cws._session_deadline(st, now) == now + 900 - cws.SETTLE_GRACE_S
    finally:
        request_lifetime.reset(token)


# ---------------------------------------------------------------------------
# offer URLs, sidecar offer cache, advertised set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "http_url,expected",
    [
        ("", "/v1/confidential/ws"),
        ("https://node.example.com", "wss://node.example.com/v1/confidential/ws"),
        ("http://httpnode.local:8000/", "ws://httpnode.local:8000/v1/confidential/ws"),
    ],
)
def test_offer_ws_url_never_uses_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, http_url: str, expected: str
) -> None:
    monkeypatch.setattr(settings, "confidential_sidecar_url", "http://10.9.8.7:7444")
    monkeypatch.setattr(settings, "http_url", http_url)
    url = conf.confidential_ws_public_url()
    assert url == expected
    assert "10.9.8.7" not in url and "7444" not in url
    assert (
        conf.confidential_offer_url() == f"{http_url.rstrip('/')}/v1/confidential/offer"
    )


def test_sidecar_ws_url_maps_the_scheme_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "confidential_sidecar_url", "https://httpside:7444/")
    assert cws._sidecar_ws_url("/zk", "a=1") == "wss://httpside:7444/zk?a=1"
    monkeypatch.setattr(settings, "confidential_sidecar_url", "http://127.0.0.1:7444")
    assert cws._sidecar_ws_url("/session", "s=1") == "ws://127.0.0.1:7444/session?s=1"


async def test_sidecar_offer_failure_is_negatively_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetches: list[int] = []

    async def failing() -> dict[str, Any]:
        fetches.append(1)
        raise conf.OfferError("unreachable")

    monkeypatch.setattr(conf, "fetch_sidecar_offer", failing)
    assert await conf.models_fields() == {}
    assert await conf.models_fields() == {}
    assert len(fetches) == 1


@pytest.mark.parametrize("node_settles", [True, False, None, 0, 1, "false", "true"])
async def test_sidecar_without_node_settles_is_refused(
    monkeypatch: pytest.MonkeyPatch, node_settles: Any
) -> None:
    """A sidecar that signs its own stub receipts never hands the node the
    usage it settles on, so every session would be charged its reservation.
    Only an explicit ``true`` is accepted: a missing or merely falsy value is
    refused too."""
    offer: dict[str, Any] = {
        "upstream_host": "api.venice.ai",
        "head_template": "x",
        "v": 2,
    }
    if node_settles is not None:
        offer["node_settles"] = node_settles

    class Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return offer

    class Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, *a: Any, **k: Any) -> Resp:
            return Resp()

    monkeypatch.setattr(settings, "confidential_sidecar_url", "http://127.0.0.1:7444")
    monkeypatch.setattr(conf.httpx, "AsyncClient", Client)
    if node_settles is True:
        assert await conf.fetch_sidecar_offer() == offer
    else:
        with pytest.raises(conf.OfferRefused):
            await conf.fetch_sidecar_offer()


async def test_last_good_sidecar_offer_survives_a_failed_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = {"upstream_host": "api.venice.ai", "head_template": "x"}
    state = {"fail": False}

    async def fetch() -> dict[str, Any]:
        if state["fail"]:
            raise conf.OfferError("down")
        return good

    monkeypatch.setattr(conf, "fetch_sidecar_offer", fetch)
    assert await conf.cached_sidecar_offer() == good
    conf._SIDECAR_CACHE["at"] -= conf._SIDECAR_OFFER_TTL_S + 1  # expire it
    state["fail"] = True
    assert await conf.cached_sidecar_offer() == good  # stale but served
    conf._SIDECAR_CACHE["at"] -= conf._SIDECAR_OFFER_STALE_S
    conf._SIDECAR_CACHE["failed_at"] = None
    with pytest.raises(conf.OfferError):
        await conf.cached_sidecar_offer()


async def test_models_field_and_price_list_share_one_predicate(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    async def side() -> dict[str, Any]:
        return {"upstream_host": "api.venice.ai", "head_template": "x"}

    def entry(mid: str, cand: Any) -> dict[str, float]:
        if mid == "raises":
            raise ValueError("no pricing")
        if mid == "nan":
            return {"in": math.nan, "cached_in": 1.0, "out": 1.0}
        return dict(RATES)

    monkeypatch.setattr(conf, "cached_sidecar_offer", side)
    monkeypatch.setattr(
        conf, "offered_models", lambda s: {m: object() for m in ("ok", "raises", "nan")}
    )
    monkeypatch.setattr(conf, "price_entry", entry)
    fields = await conf.models_fields()
    offer = await conf.build_offer()
    assert set(fields) == set(offer["price_list"]) == {"ok"}


async def test_signed_offers_are_remembered_by_sig(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    async def side() -> dict[str, Any]:
        return {"upstream_host": "api.venice.ai", "head_template": "x"}

    rates = {"m": dict(RATES)}
    monkeypatch.setattr(conf, "cached_sidecar_offer", side)
    monkeypatch.setattr(conf, "offered_models", lambda s: {"m": object()})
    monkeypatch.setattr(conf, "price_entry", lambda mid, c: dict(rates["m"]))

    first = await conf.build_offer()
    again = await conf.build_offer()
    assert again["sig"] == first["sig"]  # unchanged offer, same signature
    assert conf.signed_offer_rates(first["sig"], "m") == RATES
    assert conf.signed_offer_rates(first["sig"], "other") is None
    assert conf.signed_offer_rates("ff" * 64, "m") is None

    rates["m"] = {"in": 1.0, "cached_in": 1.0, "out": 1.0}
    newer = await conf.build_offer()
    assert newer["sig"] != first["sig"]
    assert conf.signed_offer_rates(first["sig"], "m") == RATES  # still honoured

    monkeypatch.setattr(conf, "SIGNED_OFFER_MAX", 1)
    conf._remember_offer("cd" * 64, {})
    assert conf.signed_offer_rates(first["sig"], "m") is None  # bounded


def test_signed_offer_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    conf._remember_offer("ab" * 64, {"m": dict(RATES)})
    now = conf.time.monotonic()
    monkeypatch.setattr(
        conf.time, "monotonic", lambda: now + conf.SIGNED_OFFER_TTL_S + 1
    )
    assert conf.signed_offer_rates("ab" * 64, "m") is None


# ---------------------------------------------------------------------------
# billing at frozen rates (real routstr helpers, in-memory database)
# ---------------------------------------------------------------------------


def test_frozen_cost() -> None:
    rates = {"in": 10_000.0, "cached_in": 2_500.0, "out": 100_000.0}
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 8,
        "prompt_tokens_details": {"cached_tokens": 200},
    }
    # 800*10 + 200*2.5 + 8*100 msats
    assert conf.frozen_cost_msats(rates, usage, 10**9) == 9300
    assert conf.frozen_cost_msats(rates, usage, 5000) == 5000  # capped
    # cached_in above in is never charged; cached is clamped to prompt
    pricey = {**rates, "cached_in": 50_000.0}
    over = {**usage, "prompt_tokens_details": {"cached_tokens": 10**6}}
    assert conf.frozen_cost_msats(pricey, over, 10**9) == 10_800
    assert conf.frozen_cost_msats(rates, {"total_tokens": 3}, 10**9) is None
    assert conf.frozen_cost_msats(rates, None, 10**9) is None
    for bad in (None, "12", 1.5, -1, True):
        odd = {"prompt_tokens": bad, "completion_tokens": 3}
        assert conf.frozen_cost_msats(rates, odd, 10**9) is None
    # Never above the client's bound ceil((in*prompt + out*completion)/1000).
    bound = math.ceil((rates["in"] * 1000 + rates["out"] * 8) / 1000)
    cost = conf.frozen_cost_msats(pricey, usage, 10**9)
    assert cost is not None and cost <= bound


def test_frozen_cost_bills_cache_writes() -> None:
    rates = {"in": 10_000.0, "cached_in": 2_500.0, "cache_write": 12_500.0}
    rates["out"] = 100_000.0
    # Venice: the writes inside prompt_tokens, nested and repeated top-level.
    venice = {
        "prompt_tokens": 1000,
        "completion_tokens": 8,
        "prompt_tokens_details": {
            "cached_tokens": 200,
            "cache_creation_input_tokens": 600,
        },
        "cache_creation_input_tokens": 600,
    }
    # 200*10 + 200*2.5 + 600*12.5 + 8*100 msats
    assert conf.frozen_cost_msats(rates, venice, 10**9) == 10_800
    # Other spellings, nested or top-level only.
    for usage in (
        {
            **venice,
            "prompt_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 600},
        },
        {
            **venice,
            "prompt_tokens_details": {
                "cached_tokens": 200,
                "cache_creation_tokens": 600,
            },
        },
        {**venice, "prompt_tokens_details": {"cached_tokens": 200}},
    ):
        assert conf.frozen_cost_msats(rates, usage, 10**9) == 10_800
    # An offer signed without a cache-write rate bills writes at ``in``.
    old = {k: v for k, v in rates.items() if k != "cache_write"}
    assert conf.frozen_cost_msats(old, venice, 10**9) == 9300
    # Writes never exceed the uncached prompt; odd counts are ignored.
    huge = {**venice, "cache_creation_input_tokens": 10**6, "prompt_tokens_details": {}}
    assert conf.frozen_cost_msats(rates, huge, 10**9) == math.ceil(
        (1000 * 12_500 + 8 * 100_000) / 1000
    )
    for bad in ("600", -5, True, 1.5):
        odd = {
            **venice,
            "cache_creation_input_tokens": bad,
            "prompt_tokens_details": {
                "cached_tokens": 200,
                "cache_creation_input_tokens": bad,
            },
        }
        assert conf.frozen_cost_msats(rates, odd, 10**9) == 9300


def _ctx_model() -> Any:
    """A model with a known context: context 100k, 0.01 / 0.1 sat per token."""
    return SimpleNamespace(
        id="ctx-model",
        forwarded_model_id=None,
        context_length=100_000,
        top_provider=SimpleNamespace(
            context_length=100_000, max_completion_tokens=None
        ),
        sats_pricing=SimpleNamespace(
            prompt=0.01,
            completion=0.1,
            input_cache_read=0,
            input_cache_write=0,
            max_cost=1100.0,
            max_prompt_cost=1000.0,
            max_completion_cost=100.0,
        ),
    )


def _current_sig(model_id: str = "ctx-model") -> str:
    """Sign (remember) an offer with the node's current settings, as the
    offer endpoint would, and return its signature for ``reserve``."""
    cand = conf.offered_models({})[model_id]
    pct = conf.rate_surcharge_pct()
    entry = conf.usable_price_entry(model_id, cand)
    terms = conf.OfferTerms(
        {model_id: conf.surcharged(entry, pct)} if entry else {},
        conf.session_fee_msat(),
        pct,
        conf.abandon_fee_msat(),
        conf.reserve_margin_tokens(),
    )
    sig = hashlib.sha256(repr(terms).encode()).hexdigest() * 2
    conf._remember_offer(sig, terms)
    return sig


@pytest.fixture()
async def billing_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """Real reserve/settle against an in-memory database and a fake model."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel
    from sqlmodel.ext.asyncio.session import AsyncSession

    import routstr.auth as auth_module
    import routstr.core.db as db
    from routstr.core.db import ApiKey

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def create_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    async def validate_bearer_key(auth: str, session: Any) -> Any:
        return await session.get(ApiKey, auth)

    async def side() -> dict[str, Any]:
        return {"upstream_host": "api.venice.ai"}

    model = _ctx_model()
    cand = conf.Candidate(model, _Up("https://api.venice.ai/api/v1"))
    monkeypatch.setattr(db, "create_session", create_session)
    monkeypatch.setattr(auth_module, "validate_bearer_key", validate_bearer_key)
    monkeypatch.setattr("routstr.upstream.ehbp.ROUTSTR_FEE_PERCENT", 0)
    monkeypatch.setattr(conf, "cached_sidecar_offer", side)
    monkeypatch.setattr(conf, "offered_models", lambda s: {"ctx-model": cand})
    monkeypatch.setattr(settings, "fixed_pricing", False)
    monkeypatch.setattr(settings, "tolerance_percentage", 0)
    monkeypatch.setattr(settings, "min_request_msat", 1)
    # The sums below reserve exactly the pinned cap; the completion margin
    # has its own tests.
    monkeypatch.setattr(settings, "confidential_reserve_margin_tokens", 0)

    async with create_session() as session:
        session.add(ApiKey(hashed_key="hk", balance=3_000_000))
        await session.commit()

    async def key() -> Any:
        async with create_session() as session:
            return await session.get(ApiKey, "hk")

    yield SimpleNamespace(model=model, key=key, create_session=create_session)
    for release_id in list(auth_module._reservation_heartbeats):
        await auth_module._stop_reservation_heartbeat(release_id)
    await engine.dispose()


async def test_reservation_covers_the_full_context(billing_db: Any) -> None:
    """The prompt is opaque, so the full context is reserved."""
    from routstr.payment.helpers import (
        calculate_discounted_max_cost,
        get_max_cost_for_model,
    )

    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    # 100000 tokens * 10 msats + 8 tokens * 100 msats
    assert billing.reserved_msats == 1_000_800
    assert billing.rates == {
        "in": 10_000.0,
        "cached_in": 10_000.0,
        "cache_write": 10_000.0,
        "out": 100_000.0,
    }
    key = await billing_db.key()
    assert key.reserved_balance == 1_000_800

    # The visible-body helper on the metadata-only body is the bug this avoids.
    metadata_only = await calculate_discounted_max_cost(
        await get_max_cost_for_model("ctx-model", None, billing_db.model),  # type: ignore[arg-type]
        {"model": "ctx-model", "max_tokens": 8, "max_completion_tokens": 8},
        billing_db.model,
    )
    assert metadata_only < 10_000 < billing.reserved_msats


async def test_reservation_ignores_the_models_completion_limit(
    billing_db: Any,
) -> None:
    """A pinned cap below the model's completion limit must not shrink the
    prompt reservation: the provider accepts a prompt up to context - cap, and
    the charge is capped at the reservation, so the difference would be free."""
    billing_db.model.top_provider.max_completion_tokens = 50_000
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    # 100000 tokens * 10 msats + 8 tokens * 100 msats, not (100000 - 50000) * 10
    assert billing.reserved_msats == 1_000_800
    assert conf.prompt_token_limit(billing_db.model) == 100_000
    # The catalogue's top provider and the model disagree: the larger counts.
    billing_db.model.top_provider.context_length = 64_000
    billing_db.model.context_length = 128_000
    assert conf.prompt_token_limit(billing_db.model) == 128_000
    billing_db.model.top_provider.context_length = 200_000
    assert conf.prompt_token_limit(billing_db.model) == 200_000
    # Only a completion limit, no context length: fall back to the max cost.
    billing_db.model.top_provider.context_length = None
    billing_db.model.context_length = None
    assert conf.prompt_token_limit(billing_db.model) is None


async def test_reservation_without_context_uses_the_max_cost(
    billing_db: Any,
) -> None:
    billing_db.model.top_provider = None
    billing_db.model.context_length = None
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    assert billing.reserved_msats == 1_100_000  # sats_pricing.max_cost


async def test_fixed_pricing_refuses_confidential_setups(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    """A flat per-request price cannot cover an opaque full-context request:
    an offer signed before fixed pricing was switched on is not honoured."""
    sig = _current_sig()
    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_cost_per_request", 7)
    with pytest.raises(conf.BillingError) as exc:
        await conf.reserve("hk", "ctx-model", 8, sig)
    assert exc.value.status == 503 and "FIXED_PRICING" in str(exc.value)
    with pytest.raises(conf.BillingError):
        await conf.reserve_image("hk", "img", "webp", 1, sig)
    key = await billing_db.key()
    assert key.reserved_balance == 0


async def test_fixed_pricing_serves_no_offer(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
) -> None:
    from fastapi import HTTPException

    sidecar_calls: list[int] = []

    async def side() -> dict[str, Any]:
        sidecar_calls.append(1)
        return {"upstream_host": "api.venice.ai"}

    monkeypatch.setattr(conf, "cached_sidecar_offer", side)
    monkeypatch.setattr(conf, "offered_models", lambda s: {"m": object()})
    monkeypatch.setattr(conf, "price_entry", lambda mid, c: dict(RATES))
    monkeypatch.setattr(settings, "fixed_pricing", True)
    seen = _capture_warnings(monkeypatch, conf.logger)
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            await conf.confidential_offer()
        assert exc.value.status_code == 503
        assert "FIXED_PRICING" in str(exc.value.detail)
    assert await conf.models_fields() == {}
    # Logged once, not on every offer or /v1/models request.
    assert sum("FIXED_PRICING" in m for m, _e in seen) == 1
    assert sidecar_calls == []
    monkeypatch.setattr(settings, "fixed_pricing", False)
    assert set(await conf.models_fields()) == {"m"}


async def test_settlement_uses_the_frozen_rates(billing_db: Any) -> None:
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    billing_db.model.sats_pricing.prompt = 0.05  # live rate moves after setup
    usage = {
        "model": "ctx-model",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 8,
            "prompt_tokens_details": {"cached_tokens": 200},
        },
    }
    out = await conf.settle(billing, usage)
    # 800*10 + 200*10 + 8*100 msats at the rates frozen at setup
    assert out["cost_msats"] == 10_800
    assert out["balance_msats"] == 3_000_000 - 10_800
    key = await billing_db.key()
    assert key.reserved_balance == 0 and key.total_spent == 10_800


async def test_reserve_at_the_signed_offer_rates(billing_db: Any) -> None:
    signed = {"in": 20_000.0, "cached_in": 20_000.0, "out": 100_000.0}
    conf._remember_offer("ab" * 64, {"ctx-model": signed})
    billing = await conf.reserve("hk", "ctx-model", 8, "ab" * 64)
    assert billing.rates == signed
    assert billing.reserved_msats == 2_000_800

    with pytest.raises(conf.BillingError) as exc:
        await conf.reserve("hk", "ctx-model", 8, "cd" * 64)
    assert exc.value.status == 409
    detail: Any = exc.value.detail
    assert detail["error"]["type"] == "offer_expired"


async def test_reservation_covers_a_provider_overshoot_of_the_cap(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    """A provider may bill a few completion tokens past max_tokens. The charge
    is capped at the reservation, so without a margin a full-context prompt
    plus that overshoot would go partly unbilled."""
    usage = {
        "model": "ctx-model",
        "usage": {"prompt_tokens": 100_000 - 3, "completion_tokens": 6},
    }
    # 99997*10 + 6*100 msats
    full = 1_000_570

    billing = await conf.reserve("hk", "ctx-model", 3, _current_sig())
    assert billing.reserved_msats == 1_000_300
    assert (await conf.settle(billing, usage))["cost_msats"] < full

    monkeypatch.setattr(settings, "confidential_reserve_margin_tokens", 16)
    billing = await conf.reserve("hk", "ctx-model", 3, _current_sig())
    # 100000*10 + (3 + 16)*100 msats
    assert billing.reserved_msats == 1_001_900
    out = await conf.settle(billing, usage)
    assert out["cost_msats"] == full  # the usage, not the larger hold
    key = await billing_db.key()
    assert key.reserved_balance == 0


async def test_signed_offer_freezes_the_reserve_margin(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    conf._remember_offer(
        "ab" * 64,
        conf.OfferTerms({"ctx-model": dict(RATES)}, reserve_margin_tokens=4),
    )
    monkeypatch.setattr(settings, "confidential_reserve_margin_tokens", 500)
    billing = await conf.reserve("hk", "ctx-model", 8, "ab" * 64)
    # 100000*1 + (8 + 4)*2 msats at the signed rates
    assert billing.reserved_msats == 100_024


async def test_zero_cost_releases_and_reports_balance(billing_db: Any) -> None:
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    out = await conf.settle(
        billing,
        {"model": "ctx-model", "usage": {"prompt_tokens": 0, "completion_tokens": 0}},
    )
    assert out == {"cost_msats": 0, "balance_msats": 3_000_000}
    key = await billing_db.key()
    assert key.reserved_balance == 0


# ---------------------------------------------------------------------------
# /zk proxy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("proof", ["twopc", "zkchan", "pi_nk", "pi_c2s", "pi_c3s"])
def test_zk_proxy_passes_bytes(monkeypatch: pytest.MonkeyPatch, proof: str) -> None:
    async def echo(ws: Any) -> None:
        async for frame in ws:
            await ws.send(frame)

    with MockSidecar(echo) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        state = cws._live_sids[SID] = cws.SessionState()
        url = f"/v1/confidential/zk?proof={proof}&session_id={SID}&zk_token={state.zk_token}"
        with client.websocket_connect(url) as ws:
            payload = b"\x00\x01\x02zkmux\xff" * 100
            ws.send_bytes(payload)
            assert ws.receive_bytes() == payload


@pytest.mark.parametrize("token", ["", "t0k3n-shared-with-the-sidecar"])
def test_zk_proxy_sends_the_sidecar_token(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    seen: list[str | None] = []

    async def record(ws: Any) -> None:
        seen.append(ws.request_headers.get("Authorization"))
        async for frame in ws:
            await ws.send(frame)

    monkeypatch.setattr(settings, "confidential_sidecar_token", token)
    with MockSidecar(record) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        state = cws._live_sids[SID] = cws.SessionState()
        url = f"/v1/confidential/zk?proof=zkchan&session_id={SID}&zk_token={state.zk_token}"
        with client.websocket_connect(url) as ws:
            ws.send_bytes(b"x")
            assert ws.receive_bytes() == b"x"
    assert seen == [f"Bearer {token}" if token else None]


async def test_sidecar_offer_fetch_sends_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str | None] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(
            200, json={"head_template": "x", "v": 2, "node_settles": True}
        )

    real_client = httpx.AsyncClient

    def mocked_client(**kw: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(answer), **kw)

    monkeypatch.setattr(conf.httpx, "AsyncClient", mocked_client)
    monkeypatch.setattr(settings, "confidential_sidecar_url", "http://cu-sidecar:7444")
    monkeypatch.setattr(settings, "confidential_sidecar_token", "abc")
    await conf.fetch_sidecar_offer()
    monkeypatch.setattr(settings, "confidential_sidecar_token", "")
    await conf.fetch_sidecar_offer()
    assert seen == ["Bearer abc", None]


# ``pi_c1``/``pi_n``/``pi_c2``/``pi_c3`` belonged to the retired protocol v1.
@pytest.mark.parametrize("proof", ["nope", "pi_c1", "pi_n", "pi_c2", "pi_c3"])
def test_zk_proxy_rejects_bad_proof(
    monkeypatch: pytest.MonkeyPatch, proof: str
) -> None:
    with MockSidecar(_sidecar_script({}, [])) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        with client.websocket_connect(
            f"/v1/confidential/zk?proof={proof}&session_id={SID}"
        ) as ws:
            msg = ws.receive()
            assert msg["type"] == "websocket.close"
            assert msg["code"] == 1008


class _FakeWS:
    def __init__(
        self, peer: str, headers: dict[str, str] | list[tuple[str, str]]
    ) -> None:
        from starlette.datastructures import Headers

        lines = headers.items() if isinstance(headers, dict) else headers
        self.client = type("C", (), {"host": peer})()
        self.headers = Headers(
            raw=[(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in lines]
        )


def test_client_address_trusts_only_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "confidential_trusted_proxies", ["127.0.0.1"])
    monkeypatch.setattr(settings, "confidential_client_ip_header", "x-forwarded-for")
    # The proxy appends the real client; anything left of it is client-supplied.
    xff = _FakeWS("127.0.0.1", {"x-forwarded-for": "6.6.6.6, 198.51.100.2"})
    assert cws.client_address(xff) == "198.51.100.2"  # type: ignore[arg-type]
    # X-Real-IP is ignored unless configured (a proxy that does not set it
    # would pass a client's forged value through).
    real = _FakeWS("127.0.0.1", {"x-real-ip": "6.6.6.6"})
    assert cws.client_address(real) == "127.0.0.1"  # type: ignore[arg-type]
    # Any other peer (e.g. a Tor sidecar, or a direct client) cannot choose
    # its address by header.
    spoof = _FakeWS("10.89.0.4", {"x-forwarded-for": "203.0.113.7"})
    assert cws.client_address(spoof) == "10.89.0.4"  # type: ignore[arg-type]
    direct = _FakeWS("198.51.100.9", {})
    assert cws.client_address(direct) == "198.51.100.9"  # type: ignore[arg-type]


def test_client_address_joins_repeated_forwarded_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy that adds its own X-Forwarded-For line (rather than appending to
    the client's) still decides the address."""
    monkeypatch.setattr(settings, "confidential_trusted_proxies", ["127.0.0.1"])
    monkeypatch.setattr(settings, "confidential_client_ip_header", "x-forwarded-for")
    ws = _FakeWS(
        "127.0.0.1",
        [("X-Forwarded-For", "10.9.8.7"), ("X-Forwarded-For", "203.0.113.9")],
    )
    assert cws.client_address(ws) == "203.0.113.9"  # type: ignore[arg-type]
    ws = _FakeWS(
        "127.0.0.1",
        [
            ("X-Forwarded-For", "10.9.8.7, 10.9.8.8"),
            ("X-Forwarded-For", " 203.0.113.9"),
        ],
    )
    assert cws.client_address(ws) == "203.0.113.9"  # type: ignore[arg-type]
    # A single-value header: the last line is the proxy's.
    monkeypatch.setattr(settings, "confidential_client_ip_header", "X-Real-IP")
    ws = _FakeWS("127.0.0.1", [("X-Real-IP", "10.9.8.7"), ("X-Real-IP", "203.0.113.7")])
    assert cws.client_address(ws) == "203.0.113.7"  # type: ignore[arg-type]


def test_client_address_configured_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "confidential_trusted_proxies", ["127.0.0.1"])
    monkeypatch.setattr(settings, "confidential_client_ip_header", "X-Real-IP")
    ws = _FakeWS("127.0.0.1", {"x-real-ip": "203.0.113.7"})
    assert cws.client_address(ws) == "203.0.113.7"  # type: ignore[arg-type]


def test_client_address_self_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "confidential_client_ip_header", "x-forwarded-for")
    monkeypatch.setattr(cws, "_own_addresses", lambda: {"10.89.0.3"})
    ws = _FakeWS("10.89.0.3", {"x-forwarded-for": "203.0.113.9"})
    monkeypatch.setattr(settings, "confidential_trusted_proxies", ["127.0.0.1", "::1"])
    assert cws.client_address(ws) == "10.89.0.3"  # type: ignore[arg-type]
    monkeypatch.setattr(settings, "confidential_trusted_proxies", ["self"])
    assert cws.client_address(ws) == "203.0.113.9"  # type: ignore[arg-type]


def test_ws_proxy_rejects_session_id_with_trailing_newline(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    with _connect_raw(f"/v1/confidential/ws?session_id={uuid.uuid4()}%0A&v=2") as ws:
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == 1008
    with _connect_raw(f"/v1/confidential/zk?proof=pi_nk%0A&session_id={SID}") as ws:
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == 1008
    with _connect_raw(f"/v1/confidential/zk?proof=pi_nk&session_id={SID}%0A") as ws:
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == 1008


def _connect_raw(path: str) -> Any:
    return TestClient(_app()).websocket_connect(path)


def test_mark_used_releases_the_slot_once() -> None:
    cws._unused_by_ip.clear()
    st = cws.SessionState()
    st.unused_ip = "203.0.113.1"
    cws._unused_by_ip["203.0.113.1"] = 2
    cws._mark_used(st)
    cws._mark_used(st)  # idempotent
    assert cws._unused_by_ip == {"203.0.113.1": 1}
    cws._unused_by_ip.clear()


def test_ws_proxy_limits_unused_sessions_per_address(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A fourth unused session from one address is refused (pre-warm DoS limit)."""
    holder: dict[str, int] = {}
    with MockSidecar(_sidecar_script(holder, [])) as mock:
        _use_sidecar(monkeypatch, mock.port)
        monkeypatch.setattr(cws, "cached_sidecar_offer", _fake_sidecar_offer)
        monkeypatch.setattr(settings, "confidential_max_unused_per_ip", 3)
        client = TestClient(_app())
        cws._unused_by_ip.clear()
        cws._unused_by_ip["testclient"] = 3
        try:
            with client.websocket_connect(
                f"/v1/confidential/ws?session_id={uuid.uuid4()}&v=2"
            ) as ws:
                err = ws.receive_json()
                assert err["type"] == "error" and err["status"] == 429
                msg = ws.receive()
                assert msg["type"] == "websocket.close" and msg["code"] == 1013
        finally:
            cws._unused_by_ip.clear()


# ---------------------------------------------------------------------------
# session fee, abandon fee and rate surcharge (CONFIDENTIAL_SESSION_FEE_MSAT,
# CONFIDENTIAL_ABANDON_FEE_MSAT, CONFIDENTIAL_RATE_SURCHARGE_PCT)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fee_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "confidential_session_fee_msat", 2000)
    monkeypatch.setattr(settings, "confidential_abandon_fee_msat", 10000)
    monkeypatch.setattr(settings, "confidential_rate_surcharge_pct", 20.0)


async def _offer_with(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    async def side() -> dict[str, Any]:
        return {"upstream_host": "api.venice.ai", "head_template": "x"}

    monkeypatch.setattr(conf, "cached_sidecar_offer", side)
    monkeypatch.setattr(conf, "offered_models", lambda s: {"m": object()})
    monkeypatch.setattr(conf, "price_entry", lambda mid, c: dict(RATES))
    return await conf.build_offer()


async def test_defaults_off_offer_has_no_pricing_fields(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    offer = await _offer_with(monkeypatch)
    payload = json.loads(offer["sig_payload"])
    for field in ("session_fee_msat", "abandon_fee_msat", "rate_surcharge_pct"):
        assert field not in payload
    assert payload["price_list"]["m"] == RATES
    terms = conf.signed_offer_terms(offer["sig"])
    assert terms is not None
    assert terms.session_fee_msat == 0 and terms.abandon_fee_msat == 0


async def test_offer_publishes_fees_and_surcharged_rates(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    fee_settings: None,
) -> None:
    _secret, pub_hex = node_identity
    offer = await _offer_with(monkeypatch)
    assert _verify_schnorr(offer["sig_payload"], offer["sig"], pub_hex)
    payload = json.loads(offer["sig_payload"])
    # Signed: the client verifies the fees and the surcharge with the offer.
    assert payload["session_fee_msat"] == 2000
    assert payload["abandon_fee_msat"] == 10000
    assert payload["rate_surcharge_pct"] == 20.0
    # The price list carries the billed (surcharged) rates.
    assert payload["price_list"]["m"] == {
        k: pytest.approx(v * 1.2) for k, v in RATES.items()
    }
    terms = conf.signed_offer_terms(offer["sig"])
    assert terms is not None
    assert (
        terms.session_fee_msat,
        terms.abandon_fee_msat,
        terms.rate_surcharge_pct,
    ) == (2000, 10000, 20.0)
    assert conf.signed_offer_rates(offer["sig"], "m") == payload["price_list"]["m"]


async def test_offer_publishes_the_reserve_margin(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    offer = await _offer_with(monkeypatch)
    payload = json.loads(offer["sig_payload"])
    assert payload["reserve_margin_tokens"] == 16  # the default
    terms = conf.signed_offer_terms(offer["sig"])
    assert terms is not None and terms.reserve_margin_tokens == 16

    monkeypatch.setattr(settings, "confidential_reserve_margin_tokens", 0)
    offer = await _offer_with(monkeypatch)
    assert "reserve_margin_tokens" not in json.loads(offer["sig_payload"])
    terms = conf.signed_offer_terms(offer["sig"])
    assert terms is not None and terms.reserve_margin_tokens == 0


def test_abandon_fee_defaults_to_and_never_undercuts_the_session_fee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "confidential_session_fee_msat", 2000)
    monkeypatch.setattr(settings, "confidential_abandon_fee_msat", 0)
    assert conf.abandon_fee_msat() == 2000
    monkeypatch.setattr(settings, "confidential_abandon_fee_msat", 500)
    assert conf.abandon_fee_msat() == 2000
    monkeypatch.setattr(settings, "confidential_session_fee_msat", 0)
    assert conf.abandon_fee_msat() == 500  # an abandon fee alone is allowed


async def test_models_field_carries_the_pricing(
    monkeypatch: pytest.MonkeyPatch, fee_settings: None
) -> None:
    async def side() -> dict[str, Any]:
        return {"upstream_host": "api.venice.ai", "v": 2}

    monkeypatch.setattr(conf, "cached_sidecar_offer", side)
    monkeypatch.setattr(conf, "priced_models", lambda s: {"m": None})
    fields = await conf.models_fields()
    assert fields["m"]["session_fee_msat"] == 2000
    assert fields["m"]["abandon_fee_msat"] == 10000
    assert fields["m"]["rate_surcharge_pct"] == 20.0


async def test_reserve_requires_the_client_to_accept_the_fees(
    billing_db: Any, fee_settings: None
) -> None:
    conf._remember_offer(
        "ab" * 64,
        conf.OfferTerms(
            {"ctx-model": {**RATES}}, session_fee_msat=2000, abandon_fee_msat=10000
        ),
    )
    for accepted in ((None, None), (2000, None), (None, 10000), (2000, 9999)):
        with pytest.raises(conf.BillingError) as exc:
            await conf.reserve("hk", "ctx-model", 8, "ab" * 64, *accepted)
        assert exc.value.status == 409
        detail: Any = exc.value.detail
        assert detail["error"]["type"] == "session_fee_not_accepted"
        assert detail["error"]["session_fee_msat"] == 2000
        assert detail["error"]["abandon_fee_msat"] == 10000
    key = await billing_db.key()
    assert key.reserved_balance == 0  # nothing reserved for a refused setup
    billing = await conf.reserve("hk", "ctx-model", 8, "ab" * 64, 2000, 10000)
    assert (billing.session_fee_msat, billing.abandon_fee_msat) == (2000, 10000)


async def test_reservation_includes_the_larger_fee_and_surcharge(
    billing_db: Any, fee_settings: None
) -> None:
    # An offer signed with the current settings (+20 %, fees 2000/10000).
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig(), 2000, 10000)
    assert billing.rates["in"] == pytest.approx(12_000.0)
    # (100000 * 12 + 8 * 120) msats + max(2000, 10000)
    assert billing.reserved_msats == 1_200_960 + 10000
    key = await billing_db.key()
    assert key.reserved_balance == billing.reserved_msats


async def test_signed_offer_terms_win_over_current_settings(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    conf._remember_offer(
        "ab" * 64,
        conf.OfferTerms({"ctx-model": dict(RATES)}, session_fee_msat=500),
    )
    monkeypatch.setattr(settings, "confidential_session_fee_msat", 9999)
    # An offer with only a session fee: the abandon fee equals it.
    billing = await conf.reserve("hk", "ctx-model", 8, "ab" * 64, 500, 500)
    assert billing.session_fee_msat == 500 and billing.rates == RATES
    assert billing.abandon_fee_msat == 500


async def test_settlement_is_usage_plus_session_fee_capped(
    billing_db: Any, fee_settings: None
) -> None:
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig(), 2000, 10000)
    usage = {
        "model": "ctx-model",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 8},
    }
    out = await conf.settle(billing, usage)
    # 1000*12 + 8*120 = 12960 msats at the surcharged rates, + 2000 session fee
    # (the abandon fee is not charged for a completed request).
    assert out["cost_msats"] == 12_960 + 2000
    key = await billing_db.key()
    assert key.reserved_balance == 0 and key.total_spent == 14_960

    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig(), 2000, 10000)
    zero = {"model": "ctx-model", "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    assert (await conf.settle(billing, zero))["cost_msats"] == 2000
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig(), 2000, 10000)
    huge = {
        "model": "ctx-model",
        "usage": {"prompt_tokens": 10**7, "completion_tokens": 10**6},
    }
    assert (await conf.settle(billing, huge))["cost_msats"] == billing.reserved_msats


async def test_charge_fee_charges_exactly_the_fee(
    billing_db: Any, fee_settings: None
) -> None:
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig(), 2000, 10000)
    balance = await conf.charge_fee(billing, billing.abandon_fee_msat)
    assert balance == 3_000_000 - 10000
    key = await billing_db.key()
    assert key.reserved_balance == 0 and key.total_spent == 10000


class _FeeBilling(_Billing):
    def __init__(self) -> None:
        super().__init__()
        self.session_fee_msat = 2000
        self.abandon_fee_msat = 5000
        self.rate_surcharge_pct = 20.0


def _fee_ledger(monkeypatch: pytest.MonkeyPatch) -> _Ledger:
    ledger = _Ledger(monkeypatch)

    async def reserve(*args: Any, **kwargs: Any) -> Any:
        ledger.calls.append("reserve")
        ledger.accepted = (  # type: ignore[attr-defined]
            kwargs.get("accepted_fee_msat"),
            kwargs.get("accepted_abandon_fee_msat"),
        )
        return _FeeBilling()

    async def charge_fee(billing: Any, fee: int) -> int:
        ledger.calls.append(f"charge_fee:{fee}")
        return 98000

    monkeypatch.setattr(cws, "reserve", reserve)
    monkeypatch.setattr(cws, "charge_fee", charge_fee)
    return ledger


_FEE_SETUP = {**_SETUP, "session_fee_msat": 2000, "abandon_fee_msat": 5000}


def test_fees_are_accepted_at_setup_and_never_reach_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _fee_ledger(monkeypatch)
    holder, got = _run_session(monkeypatch, [_READY, _ATTEST, _USAGE], 3, _FEE_SETUP)
    assert ledger.accepted == (2000, 5000)  # type: ignore[attr-defined]
    assert "session_fee_msat" not in holder["setup"]
    assert "abandon_fee_msat" not in holder["setup"]
    receipt = got[-1]["receipt"]
    # The receipt names the fees and the surcharge of the frozen offer.
    assert receipt["session_fee_msat"] == 2000
    assert receipt["abandon_fee_msat"] == 5000
    assert receipt["rate_surcharge_pct"] == 20.0
    assert ledger.calls == ["reserve", "settle"]


def test_receipt_has_no_pricing_fields_by_default(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    _, got = _run_session(monkeypatch, [_READY, _ATTEST, _USAGE], 3)
    receipt = got[-1]["receipt"]
    for field in ("session_fee_msat", "abandon_fee_msat", "rate_surcharge_pct"):
        assert field not in receipt


def test_abandoned_armed_session_pays_the_abandon_fee(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Armed (sidecar ready), never used: exactly the abandon fee is charged."""
    ledger = _fee_ledger(monkeypatch)
    _run_session(monkeypatch, [_READY], 1, _FEE_SETUP)
    assert ledger.calls == ["reserve", "charge_fee:5000"]


def test_session_the_sidecar_never_accepted_is_released(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """No ``ready`` (the sidecar refused the setup): no 2PC work, no fee."""
    ledger = _fee_ledger(monkeypatch)
    refusal = {"type": "error", "status": 503, "reason": "sidecar busy"}
    _run_session(monkeypatch, [refusal], 1, _FEE_SETUP)
    assert ledger.calls == ["reserve", "release"]


def test_abort_after_release_with_fees_charges_the_reservation(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _fee_ledger(monkeypatch)
    _run_session(monkeypatch, [_READY, {"type": "body_released"}], 2, _FEE_SETUP)
    assert ledger.calls == ["reserve", "charge"]


@pytest.mark.parametrize("status", [429, 503])
def test_provider_refusal_charges_the_session_fee(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], status: int
) -> None:
    """Rate limit or capacity on the provider's side: the session fee only."""
    ledger = _fee_ledger(monkeypatch)
    usage = {"type": "usage", "sid": SID, "usage": {"error_status": status}}
    _, got = _run_session(
        monkeypatch, [_READY, {"type": "body_released"}, _ATTEST, usage], 4, _FEE_SETUP
    )
    assert ledger.calls == ["reserve", "charge_fee:2000"]
    receipt = got[-1]["receipt"]
    assert receipt["cost_msats"] == 2000 and receipt["balance_msats"] == 98000


@pytest.mark.parametrize("status", [400, 404, 422, 499])
def test_rejected_request_charges_the_abandon_fee(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], status: int
) -> None:
    """A request the provider rejected costs the abandon fee: the node did the
    session's 2PC, and an invalid body must not be a free way to make it."""
    ledger = _fee_ledger(monkeypatch)
    usage = {"type": "usage", "sid": SID, "usage": {"error_status": status}}
    _, got = _run_session(
        monkeypatch, [_READY, {"type": "body_released"}, _ATTEST, usage], 4, _FEE_SETUP
    )
    assert ledger.calls == ["reserve", "charge_fee:5000"]
    assert got[-1]["receipt"]["cost_msats"] == 5000


@pytest.mark.parametrize(
    "response_bytes,expected",
    [
        (517, "charge_fee:2000"),  # the provider answered: a refusal
        (0, "charge_fee:5000"),  # it never answered: as an abandoned session
        ("517", "charge_fee:5000"),  # malformed: no refusal is assumed
    ],
)
def test_provider_closing_before_the_body_completes(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    response_bytes: Any,
    expected: str,
) -> None:
    """A provider that refuses from the head alone and closes before the
    final body record is released: the client cannot disclose the error
    head, so the node charges the session fee rather than the abandon fee."""
    ledger = _fee_ledger(monkeypatch)
    early = {"type": "upstream_closed_early", "response_bytes": response_bytes}
    err = {"type": "error", "reason": "upstream closed the connection"}
    _, got = _run_session(monkeypatch, [_READY, early, err], 2, _FEE_SETUP)
    # The signal is the node's: the client sees only the sidecar's error.
    assert [m["type"] for m in got] == ["ready", "error"]
    assert ledger.calls == ["reserve", expected]


async def test_early_close_after_release_still_charges_the_reservation() -> None:
    st = cws.SessionState()
    st.body_released = True
    early = json.dumps({"type": "upstream_closed_early", "response_bytes": 9})
    assert await cws._on_sidecar_text(early, None, st) is None  # type: ignore[arg-type]
    assert st.upstream_refused is False


def test_error_status_classes() -> None:
    assert conf.PROVIDER_REFUSAL_STATUSES == {401, 402, 403, 429, 503}
    assert conf.PROVIDER_REFUSAL_STATUSES <= conf.UNBILLED_ERROR_STATUSES


def test_setup_rejects_bad_fee_fields() -> None:
    for name in ("session_fee_msat", "abandon_fee_msat"):
        for bad in (-1, "2000", 1.5, True):
            with pytest.raises(cws.SetupError):
                cws.validate_setup({**_SETUP, name: bad}, 4096)
    cws.validate_setup({**_SETUP, "session_fee_msat": 0, "abandon_fee_msat": 0}, 4096)


# ---------------------------------------------------------------------------
# reservation lease, reservation sizing, sidecar offer validation, admission
# ---------------------------------------------------------------------------


async def _lease_expiry(billing_db: Any, release_id: str) -> int:
    from routstr.core.db import ReservationRelease

    async with billing_db.create_session() as session:
        row = await session.get(ReservationRelease, release_id)
        assert row is not None and row.expires_at is not None
        return int(row.expires_at)


async def test_reservation_lease_outlives_the_session(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    """A short request lifetime must not let the lease lapse mid-session:
    the sweeper would release the reservation and the final charge fail."""
    import time as _time

    from routstr.core.lifecycle import request_lifetime

    monkeypatch.setattr(settings, "max_request_lifetime_seconds", 60)
    token = request_lifetime.set(cws._session_lifetime())
    try:
        billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    finally:
        request_lifetime.reset(token)
    expires_at = await _lease_expiry(billing_db, billing.snapshot.release_id)
    assert expires_at > _time.time() + (
        cws.SESSION_TIMEOUT_S + cws.POST_RELEASE_TIMEOUT_S + cws.SETTLE_GRACE_S
    )
    # The settlement at the end of the session still finds it active.
    out = await conf.settle(
        billing,
        {"model": "ctx-model", "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
    )
    assert out["cost_msats"] == 200


async def test_usage_past_the_cap_is_logged(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    """Completion tokens beyond max_tokens + the margin are logged; the charge
    is unchanged (capped at the reservation)."""
    seen: list[dict[str, Any]] = []

    def warning(msg: str, *args: Any, extra: Any = None, **kw: Any) -> None:
        if "max_tokens" in msg:
            seen.append(extra)

    monkeypatch.setattr(conf.logger, "warning", warning)
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    limit = 8 + billing.reserve_margin_tokens
    await conf.settle(
        billing,
        {
            "model": "ctx-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": limit},
        },
    )
    assert seen == []
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    await conf.settle(
        billing,
        {
            "model": "ctx-model",
            "usage": {"prompt_tokens": 1, "completion_tokens": limit + 1},
        },
    )
    assert seen and seen[0]["completion_tokens"] == limit + 1
    assert seen[0]["max_tokens"] == 8


async def test_offer_error_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    async def broken() -> dict[str, Any]:
        raise conf.OfferError("sidecar at 10.0.0.5:7444: connection refused")

    monkeypatch.setattr(conf, "build_offer", broken)
    with pytest.raises(HTTPException) as info:
        await conf.confidential_offer()
    assert info.value.status_code == 502
    assert info.value.detail == "confidential sidecar unavailable"


async def test_session_lifetime_never_shortens_the_request_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(settings, "max_request_lifetime_seconds", 5000)
    assert cws._session_lifetime().deadline >= loop.time() + 4999
    monkeypatch.setattr(settings, "max_request_lifetime_seconds", 1)
    floor = cws.SESSION_TIMEOUT_S + cws.POST_RELEASE_TIMEOUT_S + cws.SETTLE_GRACE_S
    assert cws._session_lifetime().deadline >= loop.time() + floor - 1


async def test_reservation_heartbeat_runs_for_the_session_lifetime(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    import routstr.auth as auth_module
    from routstr.core.lifecycle import request_lifetime

    monkeypatch.setattr(auth_module, "create_session", billing_db.create_session)
    monkeypatch.setattr(settings, "max_request_lifetime_seconds", 0.2)
    monkeypatch.setattr(settings, "stale_reservation_timeout_seconds", 3)

    plain = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    token = request_lifetime.set(cws._session_lifetime())
    try:
        session = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    finally:
        request_lifetime.reset(token)
    await asyncio.sleep(1.3)  # one heartbeat interval, past the request lifetime
    beats = auth_module._reservation_heartbeats
    assert plain.snapshot.release_id not in beats  # the HTTP lifetime ended
    assert not beats[session.snapshot.release_id].done()


@pytest.mark.parametrize("bad", [-5, -1000])
async def test_negative_context_length_does_not_shrink_the_reservation(
    billing_db: Any, bad: int
) -> None:
    billing_db.model.top_provider.context_length = bad
    # The model's own context length still counts.
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    assert billing.reserved_msats == 1_000_800
    await conf.release(billing)
    # With no usable context length anywhere, the model is refused rather
    # than reserved at a derived cost that the bad metadata also corrupts.
    billing_db.model.context_length = bad
    assert conf.prompt_token_limit(billing_db.model) is None
    with pytest.raises(conf.BillingError):
        await conf.reserve("hk", "ctx-model", 8, _current_sig())


async def test_reservation_without_context_is_undiscounted_with_margin(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    """The visible-body tolerance discount does not apply to an opaque body,
    and the completion margin is reserved as in the context-length branch."""
    billing_db.model.top_provider = None
    billing_db.model.context_length = 0
    monkeypatch.setattr(settings, "tolerance_percentage", 1.0)
    monkeypatch.setattr(settings, "confidential_reserve_margin_tokens", 10)
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig())
    # 1100 sats max cost + 10 tokens * 100 msats
    assert billing.reserved_msats == 1_101_000


@pytest.mark.parametrize(
    "patch",
    [
        {"max_tokens_cap": "lots"},
        {"max_tokens_cap": 0},
        {"max_tokens_cap": True},
        {"key_length": "42"},
        {"key_length": -1},
        {"head_template": 7},
        {"model_allowlist": "m"},
        {"model_allowlist": ["m", 3]},
        {"model_allowlist": None, "text_only_ok": {"m": "true"}},
        {"model_allowlist": None, "text_only_ok": {"m": 1}},
    ],
)
async def test_malformed_sidecar_offer_is_refused(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], patch: Any
) -> None:
    offer: dict[str, Any] = {
        "upstream_host": "api.venice.ai",
        "head_template": "x",
        "v": 2,
        "node_settles": True,
        **patch,
    }
    if offer.get("model_allowlist", ...) is None:
        del offer["model_allowlist"]

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=offer)

    real_client = httpx.AsyncClient

    def mocked_client(**kw: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(answer), **kw)

    monkeypatch.setattr(conf.httpx, "AsyncClient", mocked_client)
    monkeypatch.setattr(settings, "confidential_sidecar_url", "http://127.0.0.1:7444")
    with pytest.raises(conf.OfferRefused):
        await conf.fetch_sidecar_offer()
    # The offer endpoint answers 502, not 500.
    app = FastAPI()
    app.include_router(conf.confidential_router)
    resp = TestClient(app).get("/v1/confidential/offer")
    assert resp.status_code == 502


def test_model_allowlist_rejects_non_boolean_flags() -> None:
    with pytest.raises(conf.OfferRefused):
        conf.model_allowlist({"text_only_ok": {"m": "yes"}})
    assert conf.model_allowlist({"text_only_ok": {"m": True, "x": False}}) == {"m"}


async def test_refused_sidecar_offer_drops_the_last_good_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar restarted without node settlement must not keep being used
    on the strength of the last good offer (that is only for outages)."""
    good = {"upstream_host": "api.venice.ai", "head_template": "x"}
    state: dict[str, Any] = {"exc": None}

    async def fetch() -> dict[str, Any]:
        if state["exc"] is not None:
            raise state["exc"]
        return dict(good)

    monkeypatch.setattr(conf, "fetch_sidecar_offer", fetch)
    assert await conf.cached_sidecar_offer() == good
    conf._SIDECAR_CACHE["at"] -= conf._SIDECAR_OFFER_TTL_S + 1
    state["exc"] = httpx.ConnectError("down")
    assert await conf.cached_sidecar_offer() == good  # an outage: still served
    conf._SIDECAR_CACHE["failed_at"] -= conf._SIDECAR_FAILURE_TTL_S + 1
    state["exc"] = conf.OfferRefused("no --node-settles")
    with pytest.raises(conf.OfferError):
        await conf.cached_sidecar_offer()


def test_trusted_proxies_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without configuration no peer, not even loopback, can name the client
    address by header."""
    from routstr.core.settings import Settings

    monkeypatch.delenv("CONFIDENTIAL_TRUSTED_PROXIES", raising=False)
    default = Settings().confidential_trusted_proxies
    assert default == []
    monkeypatch.setattr(settings, "confidential_trusted_proxies", default)
    monkeypatch.setattr(settings, "confidential_client_ip_header", "x-forwarded-for")
    ws = _FakeWS("127.0.0.1", {"x-forwarded-for": "203.0.113.5"})
    assert cws.client_address(ws) == "127.0.0.1"  # type: ignore[arg-type]


def test_sidecar_url_is_env_only() -> None:
    from routstr.core.settings import ENV_ONLY_FIELDS

    assert "confidential_sidecar_url" in ENV_ONLY_FIELDS


def test_zk_proxy_admits_only_live_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    dials: list[int] = []

    async def echo(ws: Any) -> None:
        dials.append(1)
        async for frame in ws:
            await ws.send(frame)

    with MockSidecar(echo) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        url = f"/v1/confidential/zk?proof=zkchan&session_id={SID}"
        with client.websocket_connect(url) as ws:
            msg = ws.receive()
            assert msg["type"] == "websocket.close" and msg["code"] == 1008
        assert dials == []  # never reached the sidecar
        state = cws._live_sids[SID] = cws.SessionState()
        # The sid alone (or with another session's token) admits nothing.
        for token in ["", cws.SessionState().zk_token]:
            with client.websocket_connect(f"{url}&zk_token={token}") as ws:
                msg = ws.receive()
                assert msg["type"] == "websocket.close" and msg["code"] == 1008
        assert dials == []
        url = f"{url}&zk_token={state.zk_token}"
        monkeypatch.setattr(cws, "_active_zk", cws.MAX_ZK_CONNS)
        with client.websocket_connect(url) as ws:
            msg = ws.receive()
            assert msg["type"] == "websocket.close" and msg["code"] == 1013
        assert dials == []


def test_zk_proxy_limits_each_session(monkeypatch: pytest.MonkeyPatch) -> None:
    dials: list[int] = []

    async def echo(ws: Any) -> None:
        dials.append(1)
        async for frame in ws:
            await ws.send(frame)

    with MockSidecar(echo) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        greedy = cws._live_sids[SID] = cws.SessionState()
        other_sid = "b" * 64
        other = cws._live_sids[other_sid] = cws.SessionState()
        # A session at its own limit is refused before the sidecar is dialed…
        greedy.zk_open = cws.MAX_ZK_CONNS_PER_SESSION
        url = f"/v1/confidential/zk?proof=zkchan&session_id={SID}&zk_token={greedy.zk_token}"
        with client.websocket_connect(url) as ws:
            msg = ws.receive()
            assert msg["type"] == "websocket.close" and msg["code"] == 1013
        assert dials == []
        # …and another session's channel still opens, and gives its slot back.
        url = f"/v1/confidential/zk?proof=zkchan&session_id={other_sid}&zk_token={other.zk_token}"
        with client.websocket_connect(url) as ws:
            ws.send_bytes(b"x")
            assert ws.receive_bytes() == b"x"
            assert other.zk_open == 1
        assert dials == [1]
    assert other.zk_open == 0 and cws._active_zk == 0


def test_zk_proxy_closes_when_its_session_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def echo(ws: Any) -> None:
        async for frame in ws:
            await ws.send(frame)

    with MockSidecar(echo) as mock:
        _use_sidecar(monkeypatch, mock.port)
        state = cws.SessionState()
        state.sid = SID
        cws._live_sids[SID] = state
        url = f"/v1/confidential/zk?proof=zkchan&session_id={SID}&zk_token={state.zk_token}"
        with TestClient(_app()) as client:
            with client.websocket_connect(url) as ws:
                ws.send_bytes(b"x")
                assert ws.receive_bytes() == b"x"
                client.portal.call(cws._forget_sid, state)  # type: ignore[union-attr]
                assert ws.receive()["type"] == "websocket.close"
            # The ended session admits no new channel either.
            with client.websocket_connect(url) as ws:
                assert ws.receive()["code"] == 1008


@pytest.mark.parametrize(
    "argv,env,workers",
    [
        (["uvicorn", "routstr.core.main:app"], {}, 1),
        (["uvicorn", "app", "--workers", "4"], {}, 4),
        (["uvicorn", "app", "--workers=2"], {}, 2),
        (["uvicorn", "app", "--workers", "1"], {}, 1),
        (["uvicorn", "app"], {"WEB_CONCURRENCY": "3"}, 3),
        (["gunicorn", "app", "-w", "2"], {}, 2),
        (["gunicorn", "app"], {"GUNICORN_CMD_ARGS": "--workers 5"}, 5),
        (["gunicorn", "app", "-w4"], {}, 4),
        (["gunicorn", "app", "-w=3"], {}, 3),
        (["/venv/lib/gunicorn/app/wsgiapp.py", "app", "-w", "2"], {}, 2),
        # ``-w`` means workers to gunicorn only.
        (["python", "app", "-w", "2"], {}, 1),
        (["python", "app", "-w2"], {}, 1),
    ],
)
def test_configured_workers(argv: list[str], env: dict[str, str], workers: int) -> None:
    assert cws._configured_workers(argv, env) == workers


def test_configured_workers_under_an_imported_gunicorn() -> None:
    # ``python -m gunicorn``: argv[0] names no launcher, but gunicorn is loaded.
    assert cws._configured_workers(["__main__.py", "app", "-w", "2"], {}, True) == 2


def test_claim_single_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import os
    import subprocess
    import sys
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(settings, "confidential_sidecar_url", "http://127.0.0.1:7444")
    monkeypatch.setattr(cws.sys, "argv", ["uvicorn", "app", "--workers", "2"])
    with pytest.raises(RuntimeError, match="single worker"):
        cws.claim_single_worker("sqlite:///x.db")
    monkeypatch.setattr(cws.sys, "argv", ["uvicorn", "app"])
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(cws, "_worker_lock_fd", None)
    cws.claim_single_worker("sqlite:///x.db")
    try:
        cws.claim_single_worker("sqlite:///x.db")  # the same process again
        # Another process of the same node on this host is refused.
        code = (
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except BlockingIOError:\n"
            "    sys.exit(3)\n"
        )
        (lock,) = list(tmp_path.glob("routstr-confidential-*/*.lock"))
        assert (lock.parent.stat().st_mode & 0o777) == 0o700
        assert subprocess.run([sys.executable, "-c", code, str(lock)]).returncode == 3
    finally:
        os.close(cws._worker_lock_fd)  # type: ignore[arg-type]
        monkeypatch.setattr(cws, "_worker_lock_fd", None)


def test_worker_lock_needs_a_private_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import os
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(cws.sys, "argv", ["uvicorn", "app"])
    monkeypatch.setattr(cws, "_worker_lock_fd", None)
    lock_dir = tmp_path / f"routstr-confidential-{os.getuid()}"
    # Another account's (here: a world-writable) directory is refused…
    lock_dir.mkdir(mode=0o777)
    os.chmod(lock_dir, 0o777)
    with pytest.raises(RuntimeError, match="private directory"):
        cws.claim_single_worker("sqlite:///x.db")
    # …and so is a symlink in its place, even to a private directory.
    lock_dir.rmdir()
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    lock_dir.symlink_to(target)
    with pytest.raises(RuntimeError, match="private directory"):
        cws.claim_single_worker("sqlite:///x.db")
    assert cws._worker_lock_fd is None


def test_session_registers_its_sid_for_the_zk_route(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    seen: dict[str, Any] = {}

    async def handler(ws: Any) -> None:
        async for frame in ws:
            if isinstance(frame, str) and json.loads(frame).get("type") == "setup":
                await ws.send(json.dumps(_READY))
                await asyncio.sleep(0.3)
                return

    with MockSidecar(handler) as mock:
        _use_sidecar(monkeypatch, mock.port)
        with _connect(TestClient(_app())) as ws:
            ws.send_text(json.dumps(_SETUP))
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready"
            seen["live"] = SID in cws._live_sids
            # The client gets the /zk token of its own session.
            assert ready["zk_token"] == cws._live_sids[SID].zk_token
            assert re.fullmatch(r"[0-9a-f]{64}", ready["zk_token"])
    assert seen["live"] is True
    assert SID not in cws._live_sids  # forgotten when the session ended


def test_setup_forwards_only_known_fields(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    holder: dict[str, Any] = {}
    setup = {**_SETUP, "armed": True, "escrow_e": 5, "extra": {"x": 1}}
    with MockSidecar(_sidecar_script(holder, [_READY])) as mock:
        _use_sidecar(monkeypatch, mock.port)
        with _connect(TestClient(_app())) as ws:
            ws.send_text(json.dumps(setup))
            ws.receive_text()
    assert holder["setup"] == {
        "type": "setup",
        "nonce_c": _SETUP["nonce_c"],
        "model": "test-model",
        "max_tokens": 12,
        "len": 150,
        "armed": True,
        "escrow": "rel-123",
    }


def test_setup_armed_must_be_boolean() -> None:
    bad = {**_SETUP, "armed": "yes"}
    with pytest.raises(cws.SetupError):
        cws.validate_setup(bad, 4096)


# ---------------------------------------------------------------------------
# confidential image generation (kind "images")
# ---------------------------------------------------------------------------

_IMAGES_EP = {
    "head_template": "POST /api/v1/image/generate HTTP/1.1\r\n\r\n",
    "billing": "flat",
    "response_shape": "venice",
    "count_key": "variants",
    "fixed_pins": [
        ["return_binary", False],
        ["enhance_prompt", False],
        ["enable_web_search", False],
        ["style_references", []],
    ],
    "param_space": {"resolution": ["1K", "2K", "4K"], "format": ["webp"]},
    "max_response_bytes": 64 << 20,
    "response_idle_s": 300,
}


def _image_model(mid: str, book: Any, ceiling_sats: float) -> Any:
    return SimpleNamespace(
        id=mid,
        architecture=SimpleNamespace(
            input_modalities=["text"], output_modalities=["image"]
        ),
        sats_pricing=SimpleNamespace(image_output=ceiling_sats),
        image_pricing=book,
    )


def _tiered_book() -> Any:
    from routstr.payment.image_pricing import ImagePriceTier, ImagePricing

    return ImagePricing(
        max_usd=0.18,
        tiers=[
            ImagePriceTier(resolution="1K", usd=0.15),
            ImagePriceTier(resolution="2K", usd=0.18),
        ],
        resolutions=["1K", "2K"],
        default_resolution="1K",
    )


def _flat_book() -> Any:
    from routstr.payment.image_pricing import ImagePriceTier, ImagePricing

    return ImagePricing(max_usd=0.01, tiers=[ImagePriceTier(usd=0.01)])


def test_image_price_axes_come_from_the_tiers_too() -> None:
    """A tier axis the book does not declare is still pinned per variant."""
    from routstr.payment.image_pricing import ImagePriceTier, ImagePricing

    space = {"resolution": ["1K", "2K"], "quality": ["low", "high"], "format": ["webp"]}
    undeclared = ImagePricing(
        max_usd=0.2,
        tiers=[
            ImagePriceTier(resolution="1K", quality="low", usd=0.05),
            ImagePriceTier(resolution="1K", quality="high", usd=0.1),
            ImagePriceTier(resolution="2K", quality="high", usd=0.2),
        ],
        default_resolution="1K",
    )
    variants = conf.image_variants(_image_model("q", undeclared, 200.0), space, 0)
    pins = sorted(tuple(map(tuple, v["pins"])) for v in variants)
    assert pins == sorted(
        [
            (("resolution", r), ("quality", q), ("format", "webp"))
            for r in ("1K", "2K")
            for q in ("low", "high")
        ]
    )
    # Every variant pins both axes, and the 1K/low one is priced at its tier.
    low = next(v for v in variants if v["id"] == "1K-low-webp")
    assert low["msat_per_image"] == 50_000
    # A tier axis the endpoint cannot pin keeps the model out of the offer.
    no_quality = {"resolution": ["1K", "2K"], "format": ["webp"]}
    assert (
        conf.image_variants(_image_model("q", undeclared, 200.0), no_quality, 0) == []
    )


def test_image_suffix_matches_the_shared_vector() -> None:
    fixed = _IMAGES_EP["fixed_pins"]
    suffix = conf.render_image_suffix(
        "z-image-turbo", "variants", 2, fixed, [["format", "webp"]]
    )
    assert suffix == (
        ',"model":"z-image-turbo","variants":2,"return_binary":false,'
        '"enhance_prompt":false,"enable_web_search":false,"style_references":[],'
        '"format":"webp"}'
    )
    # It closes the object the client's private part opened.
    assert json.loads('{"prompt":"x"' + suffix)["variants"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"billing": "metered"},
        {"head_template": ""},
        {"fixed_pins": [["variants", 1]]},  # the count key is pinned per request
        {"fixed_pins": [["a", 1], ["a", 2]]},
        {"fixed_pins": [["format", "png"]]},  # collides with param_space
        {"param_space": {"format": "webp"}},
        {"count_key": ""},
        {"response_idle_s": 0},
        {"response_idle_s": 601},
        {"response_idle_s": "300"},
        {"max_count": 0},
        {"max_count": True},
        {"max_response_bytes": -1},
        {"max_response_bytes": 1.5},
        # Fixed pins only switch extras off, under plain names.
        {"fixed_pins": [["safe_mode", False]]},
        {"fixed_pins": [["hide_watermark", False]]},
        {"fixed_pins": [["disable_filter", False]]},
        {"fixed_pins": [["no_logo", False]]},
        {"fixed_pins": [["nsfw_check", False]]},
        {"fixed_pins": [["Content_Moderation", False]]},
        {"fixed_pins": [["return_binary", True]]},
        {"fixed_pins": [["return_binary", "off"]]},
        {"fixed_pins": [["style_references", {}]]},
        {"fixed_pins": [["style_references", ["x"]]]},
        {"fixed_pins": [["steps", 0]]},
    ],
)
def test_malformed_images_endpoint_is_ignored(change: dict[str, Any]) -> None:
    side = {"endpoints": {"images": {**_IMAGES_EP, **change}}}
    assert conf.images_endpoint(side) is None
    assert conf.images_endpoint({"endpoints": {"images": _IMAGES_EP}}) is not None
    assert conf.images_endpoint({}) is None


def test_image_variants_per_tier_flat_and_refused_books() -> None:
    from routstr.payment.image_pricing import ImagePricing

    space = _IMAGES_EP["param_space"]
    tiered = conf.image_variants(_image_model("np", _tiered_book(), 216.0), space, 0)
    assert tiered == [
        {
            "id": "1K-webp",
            "pins": [["resolution", "1K"], ["format", "webp"]],
            "msat_per_image": 180_000,
        },
        {
            "id": "2K-webp",
            "pins": [["resolution", "2K"], ["format", "webp"]],
            "msat_per_image": 216_000,
        },
    ]
    flat = conf.image_variants(_image_model("z", _flat_book(), 12.0), space, 20.0)
    assert flat == [
        {"id": "webp", "pins": [["format", "webp"]], "msat_per_image": 14_400}
    ]
    # An upscale price is for Venice's separate upscale call, not generation:
    # every Venice book carries one, and it must not hide the model.
    upscaled = _flat_book().copy(update={"upscale": {"2x": 0.02, "4x": 0.08}})
    assert conf.image_variants(_image_model("z", upscaled, 12.0), space, 20.0) == flat
    # Prices that depend on the response, or a tier axis the endpoint cannot
    # pin, are never offered.
    for book in (
        ImagePricing(max_usd=0.04, unit="token", output_token_usd=1e-5),
        ImagePricing(max_usd=0.04, trust_upstream_cost=True),
        ImagePricing(max_usd=0.04, qualities=["low", "high"]),
    ):
        assert conf.image_variants(_image_model("x", book, 48.0), space, 0) == []
    assert (
        conf.image_variants(
            _image_model("z", _flat_book(), 12.0), {"format": ["png"]}, 0
        )
        == []
    )


def _images_world(monkeypatch: pytest.MonkeyPatch, side: dict[str, Any]) -> Any:
    import routstr.proxy as proxy

    chat = SimpleNamespace(
        id="chat-m", architecture=SimpleNamespace(output_modalities=["text"])
    )
    tiered = _image_model("nano", _tiered_book(), 216.0)
    flat = _image_model("zimg", _flat_book(), 12.0)
    inpaint = _image_model("inpaint", _flat_book(), 12.0)
    inpaint.architecture.input_modalities = ["text", "image"]
    models = {m.id: m for m in (chat, tiered, flat, inpaint)}

    async def fake_side() -> dict[str, Any]:
        return side

    monkeypatch.setattr(proxy, "get_unique_models", lambda: list(models.values()))
    monkeypatch.setattr(
        proxy,
        "get_candidates",
        lambda mid: (
            [(models[mid], _Up("https://api.venice.ai/api/v1"))]
            if mid in models
            else []
        ),
    )
    monkeypatch.setattr(conf, "cached_sidecar_offer", fake_side)
    monkeypatch.setattr(conf, "price_entry", lambda mid, c: dict(RATES))
    monkeypatch.setattr(settings, "http_url", "https://node.example.com")
    return models


async def test_offer_publishes_signed_image_variants(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _secret, pub_hex = node_identity
    side = {
        "upstream_host": "api.venice.ai",
        "head_template": "x",
        "endpoints": {"images": _IMAGES_EP},
    }
    _images_world(monkeypatch, side)
    offer = await conf.build_offer()
    assert offer["capabilities"] == ["chat", "images"]
    assert set(offer["price_list"]) == {"chat-m"}  # chat lists chat models only
    images = offer["images"]
    assert set(images["price_list"]) == {"nano", "zimg"}  # no inpaint model
    assert images["max_count"] == 4 and images["count_key"] == "variants"
    assert images["fixed_pins"] == _IMAGES_EP["fixed_pins"]
    assert images["head_template"] == _IMAGES_EP["head_template"]
    assert images["response_shape"] == "venice"
    assert images["max_response_bytes"] == 64 << 20
    assert _verify_schnorr(offer["sig_payload"], offer["sig"], pub_hex)

    assert json.loads(offer["sig_payload"])["images"] == images
    terms = conf.signed_offer_terms(offer["sig"])
    assert terms is not None and terms.images == images
    variant = conf.signed_image_variant(terms, "nano", "2K-webp")
    assert variant is not None and variant["msat_per_image"] == 216_000

    fields = await conf.models_fields()
    assert fields["nano"]["kind"] == "images" and "kind" not in fields["chat-m"]


async def test_offer_signs_the_sidecars_lower_image_count(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A sidecar that allows fewer images never gets a setup it would refuse."""
    side = {
        "upstream_host": "api.venice.ai",
        "head_template": "x",
        "endpoints": {"images": {**_IMAGES_EP, "max_count": 1}},
    }
    _images_world(monkeypatch, side)
    assert (await conf.build_offer())["images"]["max_count"] == 1


async def test_offer_without_the_images_endpoint_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _images_world(monkeypatch, {"upstream_host": "api.venice.ai", "head_template": "x"})
    offer = await conf.build_offer()
    assert "capabilities" not in offer and "images" not in offer
    fields = await conf.models_fields()
    assert set(fields) == {"chat-m"}


_IMG_SETUP = {
    "type": "setup",
    "kind": "images",
    "nonce_c": "aa" * 32,
    "model": "zimg",
    "variant": "webp",
    "count": 2,
    "len": 300,
    "auth": "sk-client-key",
    "offer_sig": "ab" * 64,
}


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"kind": "video"}, "setup.kind"),
        ({"variant": ""}, "setup.variant"),
        ({"count": 0}, "setup.count"),
        ({"count": 5}, "setup.count"),
        ({"count": True}, "setup.count"),
        ({"max_tokens": 10}, "no max_tokens"),
        ({"armed": True}, "cannot be armed"),
    ],
)
def test_image_setup_validation(change: dict[str, Any], reason: str) -> None:
    with pytest.raises(cws.SetupError) as err:
        cws.validate_setup({**_IMG_SETUP, **change}, 4096)
    assert reason in str(err.value)
    cws.validate_setup(dict(_IMG_SETUP), 4096)  # valid without max_tokens
    cws.validate_setup({**_IMG_SETUP, "armed": False}, 4096)


async def test_reserve_image_is_the_flat_price_times_count(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any, fee_settings: None
) -> None:
    side = {
        "upstream_host": "api.venice.ai",
        "head_template": "x",
        "endpoints": {"images": _IMAGES_EP},
    }
    _images_world(monkeypatch, side)
    block = conf.images_offer_block(side, conf.rate_surcharge_pct())
    assert block is not None
    terms = conf.OfferTerms({}, 2000, 20.0, 10000, 0, block)
    conf._remember_offer("cd" * 32, terms)
    # 12 sats + 20 % = 14.4 sats per image; the larger fee on top.
    billing = await conf.reserve_image("hk", "zimg", "webp", 3, "cd" * 32, 2000, 10000)
    assert billing.kind == "images" and billing.msat_per_image == 14_400
    assert billing.reserved_msats == 3 * 14_400 + 10_000
    assert billing.suffix == conf.render_image_suffix(
        "zimg", "variants", 3, _IMAGES_EP["fixed_pins"], [["format", "webp"]]
    )
    assert (await billing_db.key()).reserved_balance == billing.reserved_msats

    # Charged price × count + session fee, the rest released.
    settled = await conf.settle_flat(billing)
    assert settled["cost_msats"] == 3 * 14_400 + 2000
    key = await billing_db.key()
    assert key.reserved_balance == 0
    assert key.balance == 3_000_000 - (3 * 14_400 + 2000)

    for model, variant, count in (
        ("zimg", "2K-webp", 1),  # not a variant of this model
        ("nano", "webp", 1),
        ("unknown", "webp", 1),
    ):
        with pytest.raises(conf.BillingError) as err:
            await conf.reserve_image(
                "hk", model, variant, count, "cd" * 32, 2000, 10000
            )
        assert err.value.status == 409
    with pytest.raises(conf.BillingError):
        await conf.reserve_image("hk", "zimg", "webp", 5, "cd" * 32, 2000, 10000)
    with pytest.raises(conf.BillingError) as err:
        await conf.reserve_image("hk", "zimg", "webp", 1, "ef" * 32, 2000, 10000)
    assert err.value.status == 409
    with pytest.raises(conf.BillingError) as err:  # fees not accepted
        await conf.reserve_image("hk", "zimg", "webp", 1, "cd" * 32, None, None)
    assert err.value.status == 409


def test_4k_variants_are_one_image_at_a_time() -> None:
    from routstr.payment.image_pricing import ImagePriceTier, ImagePricing

    book = ImagePricing(
        max_usd=0.35,
        tiers=[
            ImagePriceTier(resolution="1K", usd=0.15),
            ImagePriceTier(resolution="4K", usd=0.35),
        ],
        resolutions=["1K", "4K"],
        default_resolution="1K",
    )
    variants = conf.image_variants(
        _image_model("nb", book, 420.0), _IMAGES_EP["param_space"], 0
    )
    by_id = {v["id"]: v for v in variants}
    assert by_id["4K-webp"]["max_count"] == 1
    assert "max_count" not in by_id["1K-webp"]


async def test_reserve_image_honours_the_variant_count_and_whole_prices(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any, fee_settings: None
) -> None:
    side = {
        "upstream_host": "api.venice.ai",
        "head_template": "x",
        "endpoints": {"images": _IMAGES_EP},
    }
    _images_world(monkeypatch, side)
    block = conf.images_offer_block(side, conf.rate_surcharge_pct())
    assert block is not None
    block = {**block, "price_list": dict(block["price_list"])}
    block["price_list"]["zimg"] = [
        {
            "id": "4K-webp",
            "pins": [["format", "webp"]],
            "msat_per_image": 20_000,
            "max_count": 1,
        },
        {"id": "frac-webp", "pins": [["format", "webp"]], "msat_per_image": 1.5},
    ]
    conf._remember_offer("cd" * 32, conf.OfferTerms({}, 2000, 20.0, 10000, 0, block))
    with pytest.raises(conf.BillingError) as err:
        await conf.reserve_image("hk", "zimg", "4K-webp", 2, "cd" * 32, 2000, 10000)
    assert "[1, 1]" in str(err.value)
    billing = await conf.reserve_image(
        "hk", "zimg", "4K-webp", 1, "cd" * 32, 2000, 10000
    )
    assert billing.msat_per_image == 20_000
    await conf.release(billing)
    with pytest.raises(conf.BillingError) as err:  # not a whole millisat price
        await conf.reserve_image("hk", "zimg", "frac-webp", 1, "cd" * 32, 2000, 10000)
    assert err.value.status == 409


def test_image_offer_pins_the_providers_own_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The node's public ids are lower-cased; the suffix must carry the
    provider's spelling, or the provider answers 404."""
    import routstr.proxy as proxy

    public = _image_model("wai-illustrious", _flat_book(), 12.0)
    upstream = _image_model("wai-Illustrious", _flat_book(), 12.0)
    monkeypatch.setattr(proxy, "get_unique_models", lambda: [public])
    monkeypatch.setattr(
        proxy,
        "get_candidates",
        lambda mid: [(upstream, _Up("https://api.venice.ai/api/v1"))]
        if mid.lower() == "wai-illustrious"
        else [],
    )
    side = {"upstream_host": "api.venice.ai", "endpoints": {"images": _IMAGES_EP}}
    priced = conf.priced_image_models(side, 0)
    assert list(priced) == ["wai-Illustrious"]
    # An operator-set forwarded id wins, as on the plain path.
    upstream.forwarded_model_id = "vendor/Wai"
    assert list(conf.priced_image_models(side, 0)) == ["vendor/Wai"]
    assert (
        conf.upstream_model_id(SimpleNamespace(id="", forwarded_model_id=None)) is None
    )


class _ImageBilling(_Billing):
    def __init__(self) -> None:
        super().__init__()
        self.model_id = "zimg"
        self.kind = "images"
        self.variant = "webp"
        self.count = 2
        self.msat_per_image = 14_400
        self.suffix = ',"model":"zimg","variants":2,"format":"webp"}'
        self.rates = {}


def _image_ledger(monkeypatch: pytest.MonkeyPatch) -> _Ledger:
    ledger = _Ledger(monkeypatch)

    async def reserve_image(
        auth: str,
        model: str,
        variant: str,
        count: int,
        offer_sig: str,
        accepted_fee_msat: int | None = None,
        accepted_abandon_fee_msat: int | None = None,
    ) -> Any:
        ledger.calls.append("reserve_image")
        return _ImageBilling()

    async def settle_flat(billing: Any) -> dict[str, Any]:
        ledger.calls.append("settle_flat")
        return {"cost_msats": 28_800, "balance_msats": 70_000}

    async def side() -> dict[str, Any]:
        return {"upstream_host": "api.venice.ai", "endpoints": {"images": _IMAGES_EP}}

    monkeypatch.setattr(cws, "reserve_image", reserve_image)
    monkeypatch.setattr(cws, "settle_flat", settle_flat)
    monkeypatch.setattr(cws, "cached_sidecar_offer", side)
    return ledger


_FLAT = {
    "type": "usage",
    "sid": SID,
    "model": "zimg",
    "usage": {"flat": True, "kind": "images"},
}


def test_image_session_settles_flat_with_an_image_receipt(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _secret, pub_hex = node_identity
    ledger = _image_ledger(monkeypatch)
    setup = {**_IMG_SETUP, "suffix": ',"model":"cheap"}'}  # never forwarded
    holder, got = _run_session(
        monkeypatch, [_READY, _ATTEST, {"type": "body_released"}, _FLAT], 4, setup
    )
    sent = holder["setup"]
    assert sent["kind"] == "images" and sent["count"] == 2
    assert sent["suffix"] == _ImageBilling().suffix
    assert "max_tokens" not in sent and "variant" not in sent and "auth" not in sent
    receipt_msg = got[-1]
    receipt = receipt_msg["receipt"]
    assert receipt["kind"] == "images" and receipt["variant"] == "webp"
    assert receipt["count"] == 2 and receipt["msat_per_image"] == 14_400
    assert receipt["cost_msats"] == 28_800 and "rates" not in receipt
    assert _verify_schnorr(receipt_msg["receipt_json"], receipt_msg["sig"], pub_hex)
    assert ledger.calls == ["reserve_image", "settle_flat"]


def test_image_session_on_a_node_without_images_is_refused(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    _, got = _run_session(monkeypatch, [], 1, dict(_IMG_SETUP))
    assert got[0]["status"] == 400 and "images" in got[0]["reason"]
    assert ledger.calls == []


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"error_status": 400}, "release"),
        ({"error_status": 502}, "charge"),
        (
            {"model": "zimg", "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            "charge",
        ),
        ({"flat": True, "model": "other"}, "charge"),
    ],
)
def test_image_session_other_settlements(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    usage: dict[str, Any],
    expected: str,
) -> None:
    ledger = _image_ledger(monkeypatch)
    frame = {"type": "usage", "sid": SID, "usage": usage}
    _run_session(
        monkeypatch,
        [_READY, {"type": "body_released"}, _ATTEST, frame],
        4,
        dict(_IMG_SETUP),
    )
    assert ledger.calls == ["reserve_image", expected]


def test_flat_usage_on_a_chat_session_charges_the_reservation(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    flat = {**_FLAT, "model": "test-model"}
    _run_session(monkeypatch, [_READY, {"type": "body_released"}, _ATTEST, flat], 4)
    assert ledger.calls == ["reserve", "charge"]
