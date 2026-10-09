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


def test_modality_allowed_semantics() -> None:
    assert conf.modality_allowed("m", None) is True
    assert conf.modality_allowed("m", {}) is True
    assert conf.modality_allowed("m", {"m": True}) is True
    assert conf.modality_allowed("m", {"m": False}) is False
    assert conf.modality_allowed("m", {"other": True}) is False


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
            auth: str, model: str, max_tokens: int, offer_sig: str | None = None
        ) -> Any:
            self.auth = auth
            self.offer_sig = offer_sig
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


def test_zk_proxy_passes_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def echo(ws: Any) -> None:
        async for frame in ws:
            await ws.send(frame)

    with MockSidecar(echo) as mock:
        _use_sidecar(monkeypatch, mock.port)
        client = TestClient(_app())
        url = f"/v1/confidential/zk?proof=pi_c1&session_id={SID}"
        with client.websocket_connect(url) as ws:
            payload = b"\x00\x01\x02zkmux\xff" * 100
            ws.send_bytes(payload)
            assert ws.receive_bytes() == payload


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
