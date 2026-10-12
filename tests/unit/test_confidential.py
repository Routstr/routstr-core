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
    monkeypatch.setattr(cws, "_active_sessions", 0)


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
            "suffix_keys": ["model"],
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
        "m": {"v": 1, "offer_url": "https://node.example.com/v1/confidential/offer"}
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
    ) -> None:
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
            if settle_error is not None:
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
            f"/v1/confidential/ws?session_id={uuid.uuid4()}&v=1"
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
    _, got = _run_session(monkeypatch, [_READY, {"type": "body_released"}, usage], 3)
    assert ledger.calls == ["reserve", expected]
    receipt = got[-1]["receipt"]
    assert got[-1]["type"] == "receipt"
    assert receipt["cost_msats"] == (0 if expected == "release" else 9000)
    assert receipt["balance_msats"] == (97000 if expected == "release" else 91000)


def test_usage_for_another_model_charges_the_reservation(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    other = {"type": "usage", "sid": SID, "usage": {"model": "pricier", "usage": {}}}
    _run_session(monkeypatch, [_READY, {"type": "body_released"}, other], 3)
    assert ledger.calls == ["reserve", "charge"]


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


def test_ws_proxy_requires_the_sidecar_protocol_version(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A v2 (two-party handshake) sidecar refuses v1 clients, and vice versa."""

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
            assert "version 2" in msg.get("reason", "")


def test_sidecar_protocol_version() -> None:
    assert conf.sidecar_protocol_version({}) == 1
    assert conf.sidecar_protocol_version({"v": 2}) == 2
    assert conf.sidecar_protocol_version({"v": "2"}) == 2
    assert conf.sidecar_protocol_version({"v": 9}) == 1
    assert conf.sidecar_protocol_version({"v": "x"}) == 1


def test_ws_proxy_rejects_non_uuid_session(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    holder: dict[str, int] = {}
    with MockSidecar(_sidecar_script(holder, [])) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        with client.websocket_connect(
            "/v1/confidential/ws?session_id=not-a-uuid&v=1"
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
        f"/v1/confidential/ws?session_id={uuid.uuid4()}&v=1"
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
        ("offer_sig", 5),
        ("offer_sig", "not-hex"),
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


def test_session_cap(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    _ConnectRecorder(monkeypatch)
    monkeypatch.setattr(cws, "MAX_SESSIONS", 1)
    client = TestClient(_app())
    with _connect(client) as first:
        with _connect(client) as second:
            err = json.loads(second.receive_text())
            assert err["status"] == 503
        first.send_text(json.dumps(_SETUP))
        assert json.loads(first.receive_text())["status"] == 502  # admitted
    assert cws._active_sessions == 0


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
    _, got = _run_session(monkeypatch, [_READY, {"type": "body_released"}, _USAGE], 3)
    assert got[-1]["type"] == "error" and got[-1]["status"] == 500
    assert got[-1]["detail"]["error"]["outcome"] == "charged_reservation"
    # Fallback charged the reservation; the abort path did not finalize again.
    assert ledger.calls == ["reserve", "settle", "charge"]


def test_usage_for_a_foreign_sid_is_ignored(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    foreign = {**_USAGE, "sid": "b" * 64}
    _, got = _run_session(monkeypatch, [_READY, {"type": "body_released"}, foreign], 2)
    assert [m["type"] for m in got] == ["ready", "body_released"]
    assert ledger.calls == ["reserve", "charge"]  # no disclosure for this session


def test_session_time_limit(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(cws, "SESSION_TIMEOUT_S", 0.5)

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
    # Never above the client's bound ceil((in*prompt + out*completion)/1000).
    bound = math.ceil((rates["in"] * 1000 + rates["out"] * 8) / 1000)
    cost = conf.frozen_cost_msats(pricey, usage, 10**9)
    assert cost is not None and cost <= bound


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

    async with create_session() as session:
        session.add(ApiKey(hashed_key="hk", balance=3_000_000))
        await session.commit()

    async def key() -> Any:
        async with create_session() as session:
            return await session.get(ApiKey, "hk")

    yield SimpleNamespace(model=model, key=key)
    for release_id in list(auth_module._reservation_heartbeats):
        await auth_module._stop_reservation_heartbeat(release_id)
    await engine.dispose()


async def test_reservation_covers_the_full_context(billing_db: Any) -> None:
    """The prompt is opaque, so the full context is reserved."""
    from routstr.payment.helpers import (
        calculate_discounted_max_cost,
        get_max_cost_for_model,
    )

    billing = await conf.reserve("hk", "ctx-model", 8)
    # 100000 tokens * 10 msats + 8 tokens * 100 msats
    assert billing.reserved_msats == 1_000_800
    assert billing.rates == {"in": 10_000.0, "cached_in": 10_000.0, "out": 100_000.0}
    key = await billing_db.key()
    assert key.reserved_balance == 1_000_800

    # The visible-body helper on the metadata-only body is the bug this avoids.
    metadata_only = await calculate_discounted_max_cost(
        await get_max_cost_for_model("ctx-model", None, billing_db.model),  # type: ignore[arg-type]
        {"model": "ctx-model", "max_tokens": 8, "max_completion_tokens": 8},
        billing_db.model,
    )
    assert metadata_only < 10_000 < billing.reserved_msats


async def test_reservation_without_context_uses_the_max_cost(
    billing_db: Any,
) -> None:
    billing_db.model.top_provider = None
    billing_db.model.context_length = None
    billing = await conf.reserve("hk", "ctx-model", 8)
    assert billing.reserved_msats == 1_100_000  # sats_pricing.max_cost


async def test_fixed_pricing_keeps_the_per_request_reservation(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_cost_per_request", 7)
    billing = await conf.reserve("hk", "ctx-model", 8)
    assert billing.reserved_msats == 7000


async def test_settlement_uses_the_frozen_rates(billing_db: Any) -> None:
    billing = await conf.reserve("hk", "ctx-model", 8)
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


async def test_zero_cost_releases_and_reports_balance(billing_db: Any) -> None:
    billing = await conf.reserve("hk", "ctx-model", 8)
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


@pytest.mark.parametrize("proof", ["pi_c1", "twopc", "zkchan", "pi_nk"])
def test_zk_proxy_passes_bytes(monkeypatch: pytest.MonkeyPatch, proof: str) -> None:
    async def echo(ws: Any) -> None:
        async for frame in ws:
            await ws.send(frame)

    with MockSidecar(echo) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        url = f"/v1/confidential/zk?proof={proof}&session_id={SID}"
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
        url = f"/v1/confidential/zk?proof=zkchan&session_id={SID}"
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
        return httpx.Response(200, json={"head_template": "x"})

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


def test_zk_proxy_rejects_bad_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    with MockSidecar(_sidecar_script({}, [])) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        with client.websocket_connect(
            f"/v1/confidential/zk?proof=nope&session_id={SID}"
        ) as ws:
            msg = ws.receive()
            assert msg["type"] == "websocket.close"
            assert msg["code"] == 1008


class _FakeWS:
    def __init__(self, peer: str, headers: dict[str, str]) -> None:
        self.client = type("C", (), {"host": peer})()
        self.headers = headers


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
                f"/v1/confidential/ws?session_id={uuid.uuid4()}&v=1"
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
    # Without an offer_sig the current settings apply (+20 %, fees 2000/10000).
    billing = await conf.reserve("hk", "ctx-model", 8, None, 2000, 10000)
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
    billing = await conf.reserve("hk", "ctx-model", 8, None, 2000, 10000)
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

    billing = await conf.reserve("hk", "ctx-model", 8, None, 2000, 10000)
    zero = {"model": "ctx-model", "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
    assert (await conf.settle(billing, zero))["cost_msats"] == 2000
    billing = await conf.reserve("hk", "ctx-model", 8, None, 2000, 10000)
    huge = {
        "model": "ctx-model",
        "usage": {"prompt_tokens": 10**7, "completion_tokens": 10**6},
    }
    assert (await conf.settle(billing, huge))["cost_msats"] == billing.reserved_msats


async def test_charge_fee_charges_exactly_the_fee(
    billing_db: Any, fee_settings: None
) -> None:
    billing = await conf.reserve("hk", "ctx-model", 8, None, 2000, 10000)
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


@pytest.mark.parametrize("status", [400, 429, 503])
def test_upstream_refusal_charges_the_session_fee(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], status: int
) -> None:
    """The request reached the provider: the session fee, not the abandon fee."""
    ledger = _fee_ledger(monkeypatch)
    usage = {"type": "usage", "sid": SID, "usage": {"error_status": status}}
    _, got = _run_session(
        monkeypatch, [_READY, {"type": "body_released"}, usage], 3, _FEE_SETUP
    )
    assert ledger.calls == ["reserve", "charge_fee:2000"]
    receipt = got[-1]["receipt"]
    assert receipt["cost_msats"] == 2000 and receipt["balance_msats"] == 98000


def test_setup_rejects_bad_fee_fields() -> None:
    for name in ("session_fee_msat", "abandon_fee_msat"):
        for bad in (-1, "2000", 1.5, True):
            with pytest.raises(cws.SetupError):
                cws.validate_setup({**_SETUP, name: bad}, 4096)
    cws.validate_setup({**_SETUP, "session_fee_msat": 0, "abandon_fee_msat": 0}, 4096)
