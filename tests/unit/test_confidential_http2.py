"""``http: 2`` confidential sessions (protocol v3, option A), node side.

One two-party handshake and one provider connection serve many sequential
requests. The setup reserves nothing (the node only checks the offer signature
and that the credential has funds); every ``request`` is validated, reserved
and settled the way a v2 session is.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

import routstr.confidential as conf
import routstr.confidential_ws as cws
from routstr.core.settings import settings
from routstr.nostr.sdk import generate_keypair, parse_keypair

from .test_confidential import (
    RATES,
    SID,
    MockSidecar,
    _app,
    _IdleClient,
    _QueueSidecar,
    _Up,
    _use_sidecar,
    _verify_schnorr,
)


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
    nsec, npub = generate_keypair()
    secret_hex, pub_hex = parse_keypair(nsec)
    monkeypatch.setattr(settings, "nsec", nsec)
    monkeypatch.setattr(settings, "npub", npub)
    return secret_hex, pub_hex


H2 = {"probe_path": "/api/v1/models", "idle_secs": 300, "max_requests": 100}
SIG = "ab" * 64

_SETUP = {
    "type": "setup",
    "http": 2,
    "nonce_c": "aa" * 32,
    "auth": "sk-client-key",
    "offer_sig": SIG,
}
_READY = {
    "type": "ready",
    "sid": SID,
    "pubkey": "cc" * 32,
    "head_len": 170,
    "http": 2,
    "h2": H2,
    "v": 2,
}


def _request(n: int, **over: Any) -> dict[str, Any]:
    return {
        "type": "request",
        "n": n,
        "kind": "chat",
        "model": "test-model",
        "max_tokens": 12,
        "body_len": 150,
        "auth": "sk-client-key",
        "offer_sig": SIG,
        **over,
    }


def _usage(n: int, **over: Any) -> dict[str, Any]:
    return {
        "type": "usage",
        "sid": SID,
        "http": 2,
        "n": n,
        "model": "test-model",
        "usage": {
            "model": "test-model",
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        },
        **over,
    }


def _attest(n: int) -> dict[str, Any]:
    return {
        "type": "attestation",
        "n": n,
        "a": {"sid": SID, "http": 2, "n": n, "stream": 2 * n + 1},
        "sig": "dd" * 64,
    }


def _settled(n: int) -> list[dict[str, Any]]:
    """What the sidecar sends for a request that settles on its usage."""
    return [
        {"type": "request_ok", "sid": SID, "n": n, "stream": 2 * n + 1, "suffix": "}"},
        {"type": "body_released", "n": n},
        _attest(n),
        _usage(n),
    ]


class _Snapshot:
    def __init__(self, n: int) -> None:
        self.release_id = f"rel-{n}"


class _Billing:
    def __init__(self, n: int, auth: str) -> None:
        self.key_hash = f"k-{auth}"
        self.model_id = "test-model"
        self.reserved_msats = 9000
        self.snapshot = _Snapshot(n)
        self.rates = dict(RATES)


class _Ledger:
    """Records the money calls of a session, in order."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        credential_error: Any = None,
        reserve_error_at: int | None = None,
        reserve_error: Any = None,
        offer: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.checked: list[tuple[str, str]] = []
        self.reserved: list[dict[str, Any]] = []

        async def check_credential(auth: str, offer_sig: str) -> str:
            self.calls.append("check")
            self.checked.append((auth, offer_sig))
            if credential_error is not None:
                raise credential_error
            return "k"

        async def reserve(
            auth: str,
            model: str,
            max_tokens: int,
            offer_sig: str | None = None,
            accepted_fee_msat: int | None = None,
            accepted_abandon_fee_msat: int | None = None,
        ) -> Any:
            self.calls.append("reserve")
            self.reserved.append(
                {
                    "auth": auth,
                    "model": model,
                    "max_tokens": max_tokens,
                    "offer_sig": offer_sig,
                    "fee": accepted_fee_msat,
                    "abandon": accepted_abandon_fee_msat,
                }
            )
            if reserve_error_at == len(self.reserved):
                raise reserve_error
            return _Billing(len(self.reserved), auth)

        async def settle(billing: Any, usage: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(f"settle:{billing.snapshot.release_id}")
            return {"cost_msats": 12, "balance_msats": 88000}

        async def release(billing: Any) -> int:
            self.calls.append(f"release:{billing.snapshot.release_id}")
            return 97000

        async def charge(billing: Any) -> int:
            self.calls.append(f"charge:{billing.snapshot.release_id}")
            return 91000

        async def sidecar_offer() -> dict[str, Any]:
            if offer is not None:
                return offer
            return {
                "upstream_host": "api.venice.ai",
                "max_tokens_cap": 4096,
                "http": ["1.1", "2"],
                "h2": H2,
            }

        monkeypatch.setattr(cws, "check_credential", check_credential)
        monkeypatch.setattr(cws, "reserve", reserve)
        monkeypatch.setattr(cws, "settle", settle)
        monkeypatch.setattr(cws, "release", release)
        monkeypatch.setattr(cws, "charge_reservation", charge)
        monkeypatch.setattr(cws, "cached_sidecar_offer", sidecar_offer)


def _h2_sidecar(holder: dict[str, Any], per_request: Any) -> Any:
    """A scripted sidecar: ``ready`` and ``head_written`` on the setup, then
    ``per_request(n)`` frames for each ``request``. Everything it receives is
    recorded in ``holder``."""

    async def handler(ws: Any) -> None:
        holder.setdefault("requests", [])
        holder.setdefault("other", [])
        async for frame in ws:
            if isinstance(frame, bytes):
                holder["other"].append(frame)
                continue
            msg = json.loads(frame)
            if msg.get("type") == "setup":
                holder["setup"] = msg
                await ws.send(json.dumps(_READY))
                await ws.send(json.dumps({"type": "head_written"}))
            elif msg.get("type") == "request":
                holder["requests"].append(msg)
                for out in per_request(msg["n"]):
                    await ws.send(json.dumps(out))
            else:
                holder["other"].append(msg)
        holder["closed"] = True

    return handler


class _Conn:
    """A client connection to the node, with the scripted sidecar behind it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, per_request: Any) -> None:
        self.holder: dict[str, Any] = {}
        self._mock = MockSidecar(_h2_sidecar(self.holder, per_request))
        self._monkeypatch = monkeypatch

    def __enter__(self) -> "_Conn":
        self._mock.__enter__()
        _use_sidecar(self._monkeypatch, self._mock.port)
        self._cm = TestClient(_app()).websocket_connect(
            f"/v1/confidential/ws?session_id={uuid.uuid4()}&v=2"
        )
        self.ws = self._cm.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self._cm.__exit__(None, None, None)
        finally:
            self._mock.__exit__(None, None, None)

    def send(self, msg: dict[str, Any]) -> None:
        self.ws.send_text(json.dumps(msg))

    def recv(self, count: int = 1) -> list[dict[str, Any]]:
        return [json.loads(self.ws.receive_text()) for _ in range(count)]

    def open(self) -> dict[str, Any]:
        self.send(_SETUP)
        ready, head = self.recv(2)
        assert ready["type"] == "ready" and head["type"] == "head_written"
        return ready


# ---------------------------------------------------------------------------
# offer
# ---------------------------------------------------------------------------


async def _offer(monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]) -> Any:
    async def sidecar_offer() -> dict[str, Any]:
        return {
            "upstream_host": "api.venice.ai",
            "head_template": "POST /api/v1/chat/completions HTTP/1.1\r\n\r\n",
            "key_length": 42,
            "max_tokens_cap": 4096,
            **extra,
        }

    monkeypatch.setattr(settings, "http_url", "https://node.example.com")
    cand = conf.Candidate(object(), _Up("https://api.venice.ai/api/v1"))
    monkeypatch.setattr(conf, "cached_sidecar_offer", sidecar_offer)
    monkeypatch.setattr(conf, "offered_models", lambda side: {"m": cand})
    monkeypatch.setattr(conf, "price_entry", lambda mid, c: dict(RATES))
    return await conf.build_offer()


async def test_offer_carries_the_sidecars_http_and_h2(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _secret, pub_hex = node_identity
    offer = await _offer(monkeypatch, {"http": ["1.1", "2"], "h2": H2})
    assert offer["http"] == ["1.1", "2"] and offer["h2"] == H2
    signed = json.loads(offer["sig_payload"])
    assert signed["http"] == ["1.1", "2"] and signed["h2"] == H2
    assert _verify_schnorr(offer["sig_payload"], offer["sig"], pub_hex)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"http": ["1.1"]},
        {"http": ["1.1", "2"]},
        {"h2": H2},
        {"http": "2", "h2": H2},
    ],
)
async def test_offer_without_http2_is_the_v2_offer(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], extra: dict
) -> None:
    """Without both fields the offer names neither, so a node whose sidecar
    serves only HTTP/1.1 signs exactly what it signed before."""
    offer = await _offer(monkeypatch, extra)
    assert "http" not in offer and "h2" not in offer
    assert "http" not in json.loads(offer["sig_payload"])


# ---------------------------------------------------------------------------
# shape checks
# ---------------------------------------------------------------------------


def test_setup_http_selects_the_mode() -> None:
    assert cws.setup_http({}) == 1
    assert cws.setup_http({"http": "1.1"}) == 1 and cws.setup_http({"http": 1.1}) == 1
    assert cws.setup_http({"http": 2}) == 2 and cws.setup_http({"http": "2"}) == 2
    for bad in (3, "h2", True, 1, [2], {"v": 2}):
        with pytest.raises(cws.SetupError):
            cws.setup_http({"http": bad})


@pytest.mark.parametrize(
    "change",
    [
        {"model": "m"},
        {"max_tokens": 5},
        {"kind": "chat"},
        {"len": 10},
        {"suffix": "}"},
        {"count": 1},
        {"variant": "v"},
        {"session_fee_msat": 0},
        {"nonce_c": "zz"},
        {"offer_sig": "not hex"},
        {"offer_sig": None},
        {"auth": 5},
        {"armed": "yes"},
    ],
)
def test_h2_setup_shape(change: dict[str, Any]) -> None:
    cws.validate_h2_setup(dict(_SETUP))
    cws.validate_h2_setup({**_SETUP, "armed": True})
    with pytest.raises(cws.SetupError):
        cws.validate_h2_setup({**_SETUP, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"n": 2},
        {"n": True},
        {"n": "1"},
        {"kind": "images"},
        {"model": ""},
        {"max_tokens": 0},
        {"max_tokens": 4097},
        {"max_tokens": True},
        {"body_len": 0},
        {"body_len": cws.MAX_BODY_LEN + 1},
        {"suffix": 5},
        {"offer_sig": "xyz"},
        {"auth": None},
        {"session_fee_msat": -1},
        {"abandon_fee_msat": "1"},
        {"type": "setup"},
    ],
)
def test_h2_request_shape(change: dict[str, Any]) -> None:
    cws.validate_h2_request(_request(1), 4096, 1)
    cws.validate_h2_request(_request(3, suffix="}", session_fee_msat=5), 4096, 3)
    with pytest.raises(cws.SetupError):
        cws.validate_h2_request({**_request(1), **change}, 4096, 1)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def test_setup_reserves_nothing_and_each_request_settles(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _secret, pub_hex = node_identity
    ledger = _Ledger(monkeypatch)
    with _Conn(monkeypatch, _settled) as conn:
        ready = conn.open()
        # The setup checked the credential and the offer, and reserved nothing.
        assert ledger.calls == ["check"]
        assert ledger.checked == [("sk-client-key", SIG)]
        setup = conn.holder["setup"]
        assert setup["http"] == 2 and setup["nonce_c"] == "aa" * 32
        assert "auth" not in setup and "offer_sig" not in setup
        assert setup["escrow"].startswith("h2-")
        assert ready["pubkey"] == pub_hex and ready["http"] == 2 and ready["h2"] == H2
        assert len(ready["zk_token"]) == 64

        receipts = []
        # The second request pays with another credential and accepts fees.
        extras: list[dict[str, Any]] = [
            {},
            {"auth": "cashuBfresh", "session_fee_msat": 7, "abandon_fee_msat": 9},
            {},
        ]
        for n in (1, 2, 3):
            conn.send(_request(n, **extras[n - 1]))
            ok, released, att, receipt_msg = conn.recv(4)
            assert ok["type"] == "request_ok" and ok["n"] == n
            assert released == {"type": "body_released", "n": n}
            assert _verify_schnorr(att["a_json"], att["sig"], pub_hex)
            assert att["sidecar_sig"] == "dd" * 64
            receipt = receipt_msg["receipt"]
            assert receipt["http"] == 2 and receipt["n"] == n and receipt["sid"] == SID
            assert receipt["cost_msats"] == 12 and receipt["reserved_msats"] == 9000
            assert (
                receipt["attestation_hash"]
                == hashlib.sha256(att["a_json"].encode()).hexdigest()
            )
            assert _verify_schnorr(
                receipt_msg["receipt_json"], receipt_msg["sig"], pub_hex
            )
            receipts.append(receipt)
        # Three attestations, three receipts, each bound to its own.
        assert len({r["attestation_hash"] for r in receipts}) == 3

    assert ledger.calls == [
        "check",
        "reserve",
        "settle:rel-1",
        "reserve",
        "settle:rel-2",
        "reserve",
        "settle:rel-3",
    ]
    assert [r["auth"] for r in ledger.reserved] == [
        "sk-client-key",
        "cashuBfresh",
        "sk-client-key",
    ]
    assert ledger.reserved[1]["fee"] == 7 and ledger.reserved[1]["abandon"] == 9
    assert ledger.reserved[0]["offer_sig"] == SIG
    # The sidecar saw the request terms, never the credential, the offer
    # signature or the fees.
    for n, seen in enumerate(conn.holder["requests"], start=1):
        assert seen == {
            "type": "request",
            "n": n,
            "kind": "chat",
            "model": "test-model",
            "max_tokens": 12,
            "body_len": 150,
        }


def test_records_and_control_frames_are_relayed(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    with _Conn(monkeypatch, _settled) as conn:
        conn.open()
        conn.send(_request(1))
        conn.send({"type": "ctl", "frame": "settings_ack"})
        conn.ws.send_bytes(b"\x17record")
        conn.send({"type": "release", "n": 1, "upto": 2})
        conn.recv(4)
    # In the client's order, after the request they belong to.
    assert conn.holder["other"] == [
        {"type": "ctl", "frame": "settings_ack"},
        b"\x17record",
        {"type": "release", "n": 1, "upto": 2},
    ]


def test_a_connection_that_ends_between_requests_costs_nothing(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    with _Conn(monkeypatch, _settled) as conn:
        conn.open()
        conn.send(_request(1))
        conn.recv(4)
    assert ledger.calls == ["check", "reserve", "settle:rel-1"]

    # Never used at all: nothing was reserved, nothing is charged.
    ledger = _Ledger(monkeypatch)
    with _Conn(monkeypatch, _settled) as conn:
        conn.open()
    assert ledger.calls == ["check"]


@pytest.mark.parametrize(
    "frames,expected",
    [
        # The sidecar never accepted the request.
        ([], "release:rel-2"),
        # Accepted, the body not complete at the provider (π_WIN failed, the
        # client left): released, as a v2 session before ``body_released``.
        (["request_ok"], "release:rel-2"),
        # The provider has the request and no usage was disclosed.
        (["request_ok", "body_released"], "charge:rel-2"),
        (["request_ok", "body_released", "attestation"], "charge:rel-2"),
    ],
)
def test_a_connection_that_ends_during_a_request_settles_it_as_v2(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    frames: list[str],
    expected: str,
) -> None:
    ledger = _Ledger(monkeypatch)

    def per_request(n: int) -> list[dict[str, Any]]:
        if n == 1:
            return _settled(1)
        return [f for f in _settled(2) if f["type"] in frames] + [
            {"type": "error", "reason": "proof pi_win failed"}
        ]

    with _Conn(monkeypatch, per_request) as conn:
        conn.open()
        conn.send(_request(1))
        conn.recv(4)
        conn.send(_request(2))
        got = conn.recv(len(frames) + 1)
        assert got[-1]["type"] == "error"
    # Request 1 stays settled; only request 2's reservation is decided.
    assert ledger.calls == ["check", "reserve", "settle:rel-1", "reserve", expected]


def test_abandoned_request_keeps_the_abandon_fee(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """With an abandon fee, a request the sidecar accepted and the client
    dropped before the provider had it costs that fee (v2's rule after
    ``ready``); one the sidecar never accepted is released in full."""
    for accepted, expected in ((True, ["check", "reserve", "fee:40"]), (False, None)):
        ledger = _Ledger(monkeypatch)
        real_reserve = cws.reserve

        async def reserve(*a: Any, **kw: Any) -> Any:
            billing = await real_reserve(*a, **kw)
            billing.abandon_fee_msat = 40
            billing.session_fee_msat = 0
            return billing

        async def charge_fee(billing: Any, fee: int) -> int:
            ledger.calls.append(f"fee:{fee}")
            return 1

        monkeypatch.setattr(cws, "reserve", reserve)
        monkeypatch.setattr(cws, "charge_fee", charge_fee)

        def per_request(n: int, accepted: bool = accepted) -> list[dict[str, Any]]:
            ok = [_settled(n)[0]] if accepted else []
            return ok + [{"type": "error", "reason": "bad request"}]

        with _Conn(monkeypatch, per_request) as conn:
            conn.open()
            conn.send(_request(1))
            conn.recv(2 if accepted else 1)
        assert ledger.calls == (expected or ["check", "reserve", "release:rel-1"])


def test_error_head_settles_by_the_v2_rules(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A disclosed 404 (unknown model at the provider) releases the request's
    reservation; the receipt names the request and the status."""
    ledger = _Ledger(monkeypatch)

    def per_request(n: int) -> list[dict[str, Any]]:
        if n == 1:
            return _settled(1)
        err = _usage(2, usage={"error_status": 404, "choices": []})
        return [
            _settled(2)[0],
            _attest(2),
            err,
            {"type": "closed", "reason": "error_settled"},
        ]

    with _Conn(monkeypatch, per_request) as conn:
        conn.open()
        conn.send(_request(1))
        conn.recv(4)
        conn.send(_request(2))
        _ok, _att, receipt_msg, closed = conn.recv(4)
    receipt = receipt_msg["receipt"]
    assert receipt["n"] == 2 and receipt["usage"]["error_status"] == 404
    assert receipt["cost_msats"] == 0 and receipt["balance_msats"] == 97000
    assert closed == {"type": "closed", "reason": "error_settled"}
    assert ledger.calls == [
        "check",
        "reserve",
        "settle:rel-1",
        "reserve",
        "release:rel-2",
    ]


def test_a_refused_request_reserves_nothing_and_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    detail = {"error": {"type": "insufficient_quota", "code": "insufficient_balance"}}
    ledger = _Ledger(
        monkeypatch,
        reserve_error_at=2,
        reserve_error=conf.BillingError("Insufficient balance", 402, detail),
    )
    with _Conn(monkeypatch, _settled) as conn:
        conn.open()
        conn.send(_request(1))
        conn.recv(4)
        conn.send(_request(2))
        (err,) = conn.recv(1)
        assert err["type"] == "error" and err["status"] == 402
        assert err["detail"] == detail
    # The sidecar never saw request 2; request 1 stays settled.
    assert [r["n"] for r in conn.holder["requests"]] == [1]
    assert ledger.calls == ["check", "reserve", "settle:rel-1", "reserve"]


@pytest.mark.parametrize(
    "bad,status",
    [
        (_request(2), 400),  # out of order
        ({**_request(1), "max_tokens": 0}, 400),
        ({**_request(1), "kind": "images"}, 400),
        ({k: v for k, v in _request(1).items() if k != "offer_sig"}, 400),
    ],
)
def test_a_malformed_request_is_refused_before_any_reservation(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    bad: dict[str, Any],
    status: int,
) -> None:
    ledger = _Ledger(monkeypatch)
    with _Conn(monkeypatch, _settled) as conn:
        conn.open()
        conn.send(bad)
        (err,) = conn.recv(1)
        assert err["type"] == "error" and err["status"] == status
    assert ledger.calls == ["check"]
    assert conn.holder["requests"] == []


def test_a_second_request_while_one_is_in_flight_is_refused(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)

    def per_request(n: int) -> list[dict[str, Any]]:
        return _settled(n)[:2]  # released, never disclosed

    with _Conn(monkeypatch, per_request) as conn:
        conn.open()
        conn.send(_request(1))
        conn.recv(2)
        conn.send(_request(2))
        (err,) = conn.recv(1)
        assert err["status"] == 409
    # The request in flight is finalized by the abort rule (the provider had
    # it); the refused one reserved nothing.
    assert ledger.calls == ["check", "reserve", "charge:rel-1"]
    assert [r["n"] for r in conn.holder["requests"]] == [1]


def test_frames_for_another_request_are_dropped(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A usage or attestation that names another request (or arrives with no
    request in flight) is neither signed nor billed."""
    ledger = _Ledger(monkeypatch)

    def per_request(n: int) -> list[dict[str, Any]]:
        ok, released, att, usage = _settled(n)
        stale_att = _attest(n + 1)
        stale = _usage(n + 1)
        # After the settlement nothing is in flight: a repeated usage is dropped.
        return [ok, released, stale_att, stale, att, usage, usage]

    with _Conn(monkeypatch, per_request) as conn:
        conn.open()
        conn.send(_request(1))
        got = conn.recv(4)
        assert [g["type"] for g in got] == [
            "request_ok",
            "body_released",
            "attestation",
            "receipt",
        ]
        assert json.loads(got[2]["a_json"])["n"] == 1
    assert ledger.calls == ["check", "reserve", "settle:rel-1"]


def test_usage_without_an_attestation_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)

    def per_request(n: int) -> list[dict[str, Any]]:
        ok, released, _att, usage = _settled(n)
        return [ok, released, usage]

    with _Conn(monkeypatch, per_request) as conn:
        conn.open()
        conn.send(_request(1))
        got = conn.recv(3)
        assert got[-1]["type"] == "error" and got[-1]["status"] == 502
    assert ledger.calls == ["check", "reserve", "release:rel-1"]


def _refused_setup(
    monkeypatch: pytest.MonkeyPatch, setup: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _Conn(monkeypatch, _settled) as conn:
        conn.send(setup)
        (err,) = conn.recv(1)
    return err, conn.holder


def test_setup_without_funds_never_reaches_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(
        monkeypatch,
        credential_error=conf.BillingError("Insufficient balance", 402, {"x": 1}),
    )
    err, holder = _refused_setup(monkeypatch, _SETUP)
    assert err["status"] == 402 and err["detail"] == {"x": 1}
    assert "setup" not in holder  # no session, no garbled circuit
    assert ledger.calls == ["check"]
    assert cws._active_sessions == 0 and cws._unused_by_ip == {}


def test_setup_is_refused_when_the_sidecar_offers_no_http2(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(
        monkeypatch, offer={"upstream_host": "api.venice.ai", "http": ["1.1"]}
    )
    err, holder = _refused_setup(monkeypatch, _SETUP)
    assert err["status"] == 400 and "http: 2" in err["reason"]
    assert "setup" not in holder and ledger.calls == []


def test_setup_naming_request_terms_is_refused(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    err, holder = _refused_setup(monkeypatch, {**_SETUP, "model": "test-model"})
    assert err["status"] == 400 and "setup" not in holder and ledger.calls == []
    err, _ = _refused_setup(monkeypatch, {**_SETUP, "http": 3})
    assert err["status"] == 400 and ledger.calls == []


def test_duplicate_setup_is_refused(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    with _Conn(monkeypatch, _settled) as conn:
        conn.open()
        conn.send(_SETUP)
        (err,) = conn.recv(1)
        assert err["status"] == 400 and err["reason"] == "duplicate setup"


# ---------------------------------------------------------------------------
# credential check
# ---------------------------------------------------------------------------


async def test_check_credential_needs_a_known_offer_and_funds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routstr.auth as auth
    import routstr.core.db as db

    seen: dict[str, Any] = {}

    class _Session:
        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _Key:
        hashed_key = "abc"

    async def validate(bearer: str, session: Any, min_cost: int = 0) -> Any:
        seen.update(bearer=bearer, min_cost=min_cost)
        return _Key()

    monkeypatch.setattr(db, "create_session", lambda: _Session())
    monkeypatch.setattr(auth, "validate_bearer_key", validate)
    monkeypatch.setattr(settings, "fixed_pricing", False)
    monkeypatch.setattr(settings, "min_request_msat", 3)

    # An offer this node did not sign (or that expired) is refused first.
    with pytest.raises(conf.BillingError) as unknown:
        await conf.check_credential("sk-x", SIG)
    assert unknown.value.status == 409 and seen == {}

    conf._remember_offer(SIG, conf.OfferTerms({}, session_fee_msat=50))
    assert await conf.check_credential("sk-x", SIG) == "abc"
    # The floor: the larger of the minimum request cost and the offer's fees.
    assert seen == {"bearer": "sk-x", "min_cost": 50}

    from fastapi import HTTPException

    async def broke(bearer: str, session: Any, min_cost: int = 0) -> Any:
        raise HTTPException(status_code=402, detail={"error": "no funds"})

    monkeypatch.setattr(auth, "validate_bearer_key", broke)
    with pytest.raises(conf.BillingError) as refused:
        await conf.check_credential("sk-x", SIG)
    assert refused.value.status == 402 and refused.value.detail == {"error": "no funds"}


# ---------------------------------------------------------------------------
# limits and time
# ---------------------------------------------------------------------------


def test_connection_counts_as_unused_until_its_first_request_is_released(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """An idle ``http: 2`` connection holds no reservation, so writing the
    connection head does not make it "used" (as it does for v2): it counts
    against its address's unused limit until a request reaches the provider."""
    _Ledger(monkeypatch)
    with _Conn(monkeypatch, _settled) as conn:
        conn.open()
        assert sum(cws._unused_by_ip.values()) == 1 and cws._active_sessions == 1
        conn.send(_request(1))
        conn.recv(4)
        assert cws._unused_by_ip == {} and cws._active_sessions == 1
    assert cws._active_sessions == 0 and cws._live_sids == {}


def test_unused_limit_refuses_further_connections(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    monkeypatch.setattr(settings, "confidential_max_unused_per_ip", 1)
    ledger = _Ledger(monkeypatch)
    with _Conn(monkeypatch, _settled) as first:
        first.open()
        with _Conn(monkeypatch, _settled) as second:
            (err,) = second.recv(1)
            assert err["status"] == 429
    assert ledger.calls == ["check"]


def test_deadlines_follow_the_request_not_the_connection() -> None:
    state = cws.SessionState()
    state.http2 = True
    # Handshake (or armed): v2's setup-phase limit from the accept.
    assert cws._h2_deadline(state, 100.0) == 100.0 + cws.SESSION_TIMEOUT_S
    # Idle: the sidecar's idle limit plus the grace, from the last settlement.
    state.h2_idle_since, state.h2_idle_secs = 5000.0, 120.0
    assert cws._h2_deadline(state, 100.0) == 5000.0 + 120.0 + cws.H2_IDLE_GRACE_S
    # A request in flight: v2's two phases, counted from the request.
    state.billing = _Billing(1, "a")  # type: ignore[assignment]
    state.h2_request_at = 9000.0
    assert cws._h2_deadline(state, 100.0) == 9000.0 + cws.SESSION_TIMEOUT_S
    state.released_at = 9100.0
    assert cws._h2_deadline(state, 100.0) == 9100.0 + cws.POST_RELEASE_TIMEOUT_S
    state.h2_lifetime = cws.RequestLifetime(deadline=9200.0)
    assert cws._h2_deadline(state, 100.0) == 9200.0 - cws.SETTLE_GRACE_S


async def test_idle_connection_is_cut_after_the_idle_limit(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """If the sidecar does not close an idle connection itself, the node does,
    and nothing is charged."""
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(cws, "H2_IDLE_GRACE_S", 0.1)
    sidecar = _QueueSidecar([_READY, {"type": "head_written"}], [])
    state = cws.SessionState()
    state.http2 = True
    state.h2_idle_secs = 0.2
    client = _IdleClient()
    started = asyncio.get_running_loop().time()
    await cws._run_h2_session(client, sidecar, state, started)  # type: ignore[arg-type]
    assert sidecar.closed
    assert any(m.get("status") == 408 for m in client.sent)
    assert ledger.calls == []


async def test_request_time_limit_charges_a_released_request(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A request the provider has and that is never disclosed is charged at
    its own time limit, as a v2 session is; the limit runs from the release."""
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(cws, "POST_RELEASE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(cws, "SETTLE_GRACE_S", 0.0)
    frames = [_READY, {"type": "head_written"}, *_settled(1)[:2]]
    state = cws.SessionState()
    state.http2 = True
    state.sid = SID

    class _Client(_IdleClient):
        def __init__(self) -> None:
            super().__init__()
            self.first = True

        async def receive(self) -> dict[str, Any]:
            if self.first:
                self.first = False
                return {"type": "websocket.receive", "text": json.dumps(_request(1))}
            await asyncio.Event().wait()
            return {}

    class _Sidecar(_QueueSidecar):
        async def send(self, data: Any) -> None:
            # The request arrived: answer it.
            for f in frames:
                self.queue.put_nowait(json.dumps(f))

    sidecar = _Sidecar([], [])
    client = _Client()
    await cws._run_h2_session(
        client,  # type: ignore[arg-type]
        sidecar,
        state,
        asyncio.get_running_loop().time(),
    )
    assert any(m.get("status") == 408 for m in client.sent)
    assert ledger.calls == ["reserve", "charge:rel-1"]


def test_zk_route_admits_pi_win() -> None:
    assert cws._PROOF_RE.fullmatch("pi_win")
    assert not cws._PROOF_RE.fullmatch("pi_c1")
