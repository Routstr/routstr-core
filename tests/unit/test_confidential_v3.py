"""Protocol v3 connections, node side (PROTOCOL-V3.md §3, §8, §12).

A connection is one two-party handshake; it carries requests: many, one
after another, on framing ``"2"``, exactly one on framing ``"1.1"``. The
setup reserves the connection deposit; each ``request`` is validated,
reserved and settled on its own. These tests hold the node to the money rules
of §12.10: when the deposit is released or charged, that the session fee is
due once per connection, and what a refused request leaves behind.

``test_confidential.py`` has the harness (a scripted sidecar and a ledger
that stands in for the node's billing) and the rules of a single request.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routstr.confidential as conf
import routstr.confidential_ws as cws
from routstr.core.settings import settings

# ruff: noqa: F811  (the imported fixtures are requested by name)
from .test_confidential import (  # noqa: F401
    _HEAD,
    FEES,
    H2,
    IMAGE_PRICE,
    IMAGE_SUFFIX,
    RATES,
    SID,
    SIDE_OFFER,
    SIG,
    _attest,
    _closed,
    _Conn,
    _current_sig,
    _error_usage,
    _flat_usage,
    _fresh_module_state,
    _IdleClient,
    _image_request,
    _Ledger,
    _QueueSidecar,
    _ready,
    _refused_setup,
    _released,
    _request,
    _request_ok,
    _settled,
    _setup,
    _Up,
    _usage,
    _verify_schnorr,
    billing_db,
    fee_settings,
    node_identity,
)

DEP = 700
_FEE = {"session_fee_msat": FEES[0], "abandon_fee_msat": FEES[1]}


def _deposit(frame: dict[str, Any]) -> tuple[Any, Any, Any]:
    """(outcome, reason, msat) of the deposit a last frame reports."""
    dep = frame.get("deposit") or {}
    return dep.get("outcome"), dep.get("reason"), dep.get("msat")


# ---------------------------------------------------------------------------
# offer
# ---------------------------------------------------------------------------


async def _offer(monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]) -> Any:
    async def sidecar_offer() -> dict[str, Any]:
        return {
            "v": 3,
            "twopc": "split-public-1",
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


async def test_offer_is_a_v3_offer(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _secret, pub_hex = node_identity
    offer = await _offer(monkeypatch, {"http": ["2", "1.1"], "h2": H2})
    signed = json.loads(offer["sig_payload"])
    for o in (offer, signed):
        # Exactly this version (no list), the sidecar's framings in its
        # order, its h2 block and key-schedule protocol.
        assert o["v"] == 3 and o["twopc"] == "split-public-1"
        assert o["http"] == ["2", "1.1"] and o["h2"] == H2
        # Connection terms are top-level: they hold for both framings.
        assert o["connection_deposit_msat"] == 1000 and o["idle_timeout_s"] == 120
        assert "connection_deposit_msat" not in o["h2"]
    assert _verify_schnorr(offer["sig_payload"], offer["sig"], pub_hex)
    terms = conf.signed_offer_terms(offer["sig"])
    assert terms is not None and terms.http == ("2", "1.1") and terms.h2 == H2
    assert terms.connection_deposit_msat == 1000 and terms.idle_timeout_s == 120


@pytest.mark.parametrize("extra", [{}, {"http": ["1.1"]}, {"http": ["1.1"], "h2": H2}])
async def test_offer_without_framing_2_has_no_h2_block(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], extra: dict
) -> None:
    """A sidecar that serves (or currently serves) only HTTP/1.1: the offer
    lists that framing, still with a deposit and an idle timeout."""
    offer = await _offer(monkeypatch, extra)
    assert offer["http"] == ["1.1"] and "h2" not in offer
    assert offer["v"] == 3 and offer["connection_deposit_msat"] == 1000
    assert "h2" not in json.loads(offer["sig_payload"])


@pytest.mark.parametrize(
    "extra",
    [
        {"http": []},
        {"http": "2"},
        {"http": ["2"]},  # "2" needs the h2 block
        {"http": ["2", "2"], "h2": H2},
        {"http": ["1.1", "3"]},
        {"http": [2], "h2": H2},
    ],
)
async def test_offer_refuses_a_malformed_framing_list(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], extra: dict
) -> None:
    with pytest.raises(conf.OfferRefused):
        await _offer(monkeypatch, extra)


def test_connection_deposit_default_and_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset: the largest of the abandon fee, the session fee and 1000 msat.
    Set: that value (0 = no deposit), at least the node's minimum request."""
    monkeypatch.setattr(settings, "min_request_msat", 1)
    assert conf.connection_deposit_msat() == 1000
    monkeypatch.setattr(settings, "confidential_session_fee_msat", 3000)
    assert conf.connection_deposit_msat() == 3000
    monkeypatch.setattr(settings, "confidential_abandon_fee_msat", 5000)
    assert conf.connection_deposit_msat() == 5000
    # An abandon fee below the session fee counts as the session fee.
    monkeypatch.setattr(settings, "confidential_abandon_fee_msat", 100)
    assert conf.connection_deposit_msat() == 3000
    monkeypatch.setattr(settings, "confidential_session_fee_msat", 200)
    assert conf.connection_deposit_msat() == 1000
    monkeypatch.setattr(settings, "confidential_connection_deposit_msat", 2500)
    assert conf.connection_deposit_msat() == 2500
    monkeypatch.setattr(settings, "min_request_msat", 4000)
    assert conf.connection_deposit_msat() == 4000
    monkeypatch.setattr(settings, "confidential_connection_deposit_msat", 0)
    assert conf.connection_deposit_msat() == 0


def test_idle_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    side = {"http": ["2", "1.1"], "h2": H2}
    monkeypatch.setattr(settings, "confidential_idle_timeout_s", 90)
    assert conf.idle_timeout_s(side) == 90
    # Never above the sidecar's own idle limit while it serves framing "2"…
    monkeypatch.setattr(settings, "confidential_idle_timeout_s", 900)
    assert conf.idle_timeout_s(side) == 300
    assert conf.idle_timeout_s({**side, "h2": {**H2, "idle_secs": 45}}) == 45
    # …and unbounded by it otherwise; never below 10 s.
    assert conf.idle_timeout_s({"http": ["1.1"], "h2": H2}) == 900
    monkeypatch.setattr(settings, "confidential_idle_timeout_s", 3)
    assert conf.idle_timeout_s(side) == 10


async def test_offer_lists_images_on_framing_2_only_with_an_images_block(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    side_h2 = {**H2, "kinds": ["chat", "images"]}
    # The sidecar serves images on framing "2", the node prices none: chat
    # only, and the block is the one a chat-only sidecar gives.
    offer = await _offer(monkeypatch, {"http": ["2", "1.1"], "h2": side_h2})
    assert "kinds" not in offer["h2"] and "images" not in offer
    # With priced image variants the signed block lists both kinds.
    block = {"head_template": "POST /api/v1/image/generate HTTP/1.1\r\n\r\n"}
    monkeypatch.setattr(conf, "images_offer_block", lambda side, pct: block)
    offer = await _offer(monkeypatch, {"http": ["2", "1.1"], "h2": side_h2})
    assert offer["h2"]["kinds"] == ["chat", "images"] and offer["images"] == block
    assert json.loads(offer["sig_payload"])["h2"]["kinds"] == ["chat", "images"]
    # A sidecar that keeps images on framing "1.1": the node does not add them.
    offer = await _offer(monkeypatch, {"http": ["2", "1.1"], "h2": H2})
    assert "kinds" not in offer["h2"] and offer["images"] == block
    assert conf.h2_kinds({"kinds": ["images"]}, True) == ["chat"]
    assert conf.h2_kinds({"kinds": "chat,images"}, True) == ["chat"]


def test_offer_endpoint_takes_the_version_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clients ask for ``/v1/confidential/offer?v=3`` (§11); the node ignores
    the query."""

    async def build() -> dict[str, Any]:
        return {"v": 3}

    monkeypatch.setattr(conf, "build_offer", build)
    app = FastAPI()
    app.include_router(conf.confidential_router)
    client = TestClient(app)
    for query in ("", "?v=3", "?v=2", "?v=x&other=1"):
        resp = client.get(f"/v1/confidential/offer{query}")
        assert resp.status_code == 200 and resp.json() == {"v": 3}


# ---------------------------------------------------------------------------
# the offer a connection was opened on
# ---------------------------------------------------------------------------


def test_connection_fields_leave_out_what_belongs_to_a_request() -> None:
    offer = {
        "v": 3,
        "twopc": "split-public-1",
        "upstream_host": "h",
        "head_template": "t",
        "chat": {"cap_keys": ["max_tokens"]},
        "h2": H2,
        "connection_deposit_msat": 5,
        "idle_timeout_s": 120,
        "images": {"head_template": "i", "price_list": {"m": [1]}},
        "http": ["2", "1.1"],
        "price_list": {"m": RATES},
        "session_fee_msat": 7,
    }
    base = conf.connection_fields(offer)
    # Prices, fees and the framing list are per request.
    for change in (
        {"price_list": {}},
        {"session_fee_msat": 9},
        {"abandon_fee_msat": 9},
        {"http": ["1.1"]},
        {"images": {"head_template": "i", "price_list": {}}},
    ):
        assert conf.connection_fields({**offer, **change}) == base
    for change in (
        {"v": 4},
        {"twopc": "x"},
        {"upstream_host": "g"},
        {"head_template": "u"},
        {"chat": {}},
        {"h2": {**H2, "max_requests": 5}},
        {"connection_deposit_msat": 6},
        {"idle_timeout_s": 60},
        {"images": {"head_template": "j", "price_list": {"m": [1]}}},
    ):
        assert conf.connection_fields({**offer, **change}) != base


def _terms(connection: str, http: tuple[str, ...] = ("2", "1.1")) -> conf.OfferTerms:
    return conf.OfferTerms({}, http=http, connection=connection)


def test_check_request_offer() -> None:
    opened = _terms("A")
    conf._remember_offer("aa", opened)
    conf._remember_offer("bb", _terms("A"))  # a newer offer, same connection terms
    conf._remember_offer("cc", _terms("B"))
    conf._remember_offer("dd", _terms("A", ("1.1",)))  # "2" was withdrawn
    conf.check_request_offer("aa", opened, "2")
    conf.check_request_offer("bb", opened, "2")
    conf.check_request_offer("dd", opened, "1.1")
    for sig, framing, kind in (
        ("cc", "2", "offer_changed"),
        ("dd", "2", "offer_changed"),
        ("ee", "2", "offer_expired"),
    ):
        with pytest.raises(conf.BillingError) as err:
            conf.check_request_offer(sig, opened, framing)
        assert err.value.status == 409
        assert err.value.detail["error"]["type"] == kind


def test_an_offer_stays_remembered_while_a_connection_is_open_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = {"t": 1000.0}
    monkeypatch.setattr(conf.time, "monotonic", lambda: now["t"])
    terms = _terms("A")
    conf._remember_offer("aa", terms)
    assert conf.pin_offer("zz") is None  # an offer this node does not know
    assert conf.pin_offer("aa") is terms and conf.pin_offer("aa") is terms
    now["t"] += conf.SIGNED_OFFER_TTL_S * 3
    assert conf.signed_offer_terms("aa") is terms
    conf.unpin_offer("aa")
    assert conf.signed_offer_terms("aa") is terms  # the second connection
    conf.unpin_offer("aa")
    # Then it stays for the usual time, counted from the connection's end.
    now["t"] += conf.SIGNED_OFFER_TTL_S - 1
    assert conf.signed_offer_terms("aa") is terms
    now["t"] += 2
    assert conf.signed_offer_terms("aa") is None
    conf.unpin_offer("aa")  # idempotent


def _real_offers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three offers this node signed: ``SIG`` (the connection's), ``new``
    (other prices, same connection terms), ``moved`` (another deposit)."""
    conf._remember_offer(SIG, _terms("A"))
    conf._remember_offer("ce" * 64, _terms("A"))
    conf._remember_offer("cf" * 64, _terms("B"))


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_a_request_naming_a_changed_offer_is_refused_and_the_connection_stays(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    ledger = _Ledger(monkeypatch, deposit=DEP)
    _real_offers(monkeypatch)
    with _Conn(monkeypatch, http=http) as conn:
        conn.open()
        conn.send(_request(offer_sig="cf" * 64))
        refused = conn.recv1()
        assert refused["type"] == "request_refused" and refused["status"] == 409
        assert refused["n"] == 1
        assert refused["detail"]["error"]["type"] == "offer_changed"
        conn.send(_request(offer_sig="0d" * 64))  # one the node never signed
        expired = conn.recv1()
        assert expired["type"] == "request_refused"
        assert expired["detail"]["error"]["type"] == "offer_expired"
        # A newer offer with the same connection terms, or the old one: fine.
        conn.send(_request(offer_sig="ce" * 64))
        assert conn.recv1()["type"] == "request_ok"
    assert [r["offer_sig"] for r in ledger.reserved] == ["ce" * 64]
    assert conn.holder["requests"][0]["n"] == 1
    # The connection's offer is no longer pinned once it ended.
    assert conf._PINNED_OFFERS == {}


def test_the_connection_takes_its_idle_timeout_from_its_offer(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    conf._remember_offer(SIG, conf.OfferTerms({}, idle_timeout_s=33))
    seen: dict[str, Any] = {}
    real = cws._run_h2_session

    async def spy(ws: Any, upstream: Any, state: Any, started: float) -> None:
        seen["idle"] = state.h2_idle_timeout
        seen["pinned"] = SIG in conf._PINNED_OFFERS
        await real(ws, upstream, state, started)

    monkeypatch.setattr(cws, "_run_h2_session", spy)
    with _Conn(monkeypatch, http="2") as conn:
        conn.open()
    assert seen == {"idle": 33.0, "pinned": True}
    assert conf._PINNED_OFFERS == {}


def test_a_refused_setup_unpins_its_offer(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch, conf.BillingError("no funds", 402))
    conf._remember_offer(SIG, _terms("A"))
    _refused_setup(monkeypatch, _setup())
    assert conf._PINNED_OFFERS == {}


# ---------------------------------------------------------------------------
# requests on a connection
# ---------------------------------------------------------------------------


def test_requests_on_one_framing_2_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Three requests, one handshake: each reserved and settled on its own;
    the first settlement returns the deposit and carries the session fee."""
    _secret, pub_hex = node_identity
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    receipts = []
    with _Conn(monkeypatch, lambda n: _settled(n, "2"), "2") as conn:
        ready = conn.open()
        assert ready["deposit_msat"] == DEP
        for n in (1, 2, 3):
            # Another credential and offer signature per request is allowed.
            conn.send(_request(n, auth=f"sk-{n}", **_FEE))
            ok = conn.recv1()
            assert ok["type"] == "request_ok" and ok["stream"] == 2 * n + 1
            msg = conn.until("receipt")
            assert _verify_schnorr(msg["receipt_json"], msg["sig"], pub_hex)
            receipts.append(msg["receipt"])
        assert cws._live_sids[SID].billing is None  # idle holds nothing
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve",
        "settle:rel-1",
        "release:dep",
        "reserve",
        "settle:rel-2",
        "reserve",
        "settle:rel-3",
    ]
    assert [r["auth"] for r in ledger.reserved] == ["sk-1", "sk-2", "sk-3"]
    # The session fee is due until the connection's first settlement.
    assert [r["session_fee_due"] for r in ledger.reserved] == [True, False, False]
    assert [r["n"] for r in receipts] == [1, 2, 3]
    assert [r["cost_msats"] for r in receipts] == [2012, 12, 12]
    assert [r.get("session_fee_msat", 0) for r in receipts] == [2000, 0, 0]
    assert [r.get("deposit_released_msat") for r in receipts] == [DEP, None, None]
    assert all(r["http"] == "2" and r["sid"] == SID for r in receipts)
    assert all(r["abandon_fee_msat"] == 5000 for r in receipts)
    # The sidecar saw no credential, offer signature or fee.
    assert conn.holder["requests"] == [
        {
            "type": "request",
            "n": n,
            "kind": "chat",
            "model": "test-model",
            "max_tokens": 12,
            "body_len": 150,
        }
        for n in (1, 2, 3)
    ]


def test_a_framing_11_connection_carries_one_request(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Request 1 comes before the ClientHello; after its settlement the
    sidecar closes the connection (``done``), with nothing left to say about
    the deposit."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    with _Conn(monkeypatch, lambda n: _settled(n, "1.1"), "1.1") as conn:
        ready = conn.open(_setup("1.1", armed=True))
        assert ready["deposit_msat"] == DEP and "head_len" not in ready
        conn.send(_request(**_FEE))
        ok = conn.recv1()
        assert ok["type"] == "request_ok" and ok["head_len"] == 228
        receipt = conn.until("receipt")["receipt"]
        assert conn.recv1() == {"type": "closed", "reason": "done"}
        assert conn.closed()
    assert receipt["http"] == "1.1" and receipt["n"] == 1
    assert receipt["deposit_released_msat"] == DEP and receipt["cost_msats"] == 2012
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve",
        "settle:rel-1",
        "release:dep",
    ]


def test_a_second_request_on_framing_11_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    script = lambda n: _settled(n, "1.1")[:-1]  # noqa: E731  (stays open)
    with _Conn(monkeypatch, script, "1.1") as conn:
        conn.open()
        conn.send(_request())
        conn.until("receipt")
        conn.send(_request(2))
        err = conn.recv1()
        assert err["type"] == "error" and err["status"] == 409
        assert "one request" in err["reason"]
        assert conn.closed()
    assert ledger.calls == ["check", "reserve", "settle:rel-1"]


def test_records_and_control_frames_are_relayed(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    control = [
        {"type": "settings_seen", "header_table_size": None},
        {"type": "ctl", "frame": "settings_ack"},
        {"type": "release", "n": 1, "upto": 2},
        {"type": "disclose", "n": 1, "r": "00", "tail_lens": [5]},
    ]
    with _Conn(monkeypatch, http="2") as conn:
        conn.open()
        conn.send(control[0])
        conn.send(_request())
        conn.recv1()
        conn.ws.send_bytes(b"\x17\x03\x03\x00\x01x")
        for msg in control[1:]:
            conn.send(msg)
        # The sidecar has everything before the client leaves.
        for _ in range(100):
            if len(conn.holder["other"]) == 5:
                break
            time.sleep(0.02)
    assert conn.holder["other"] == [
        control[0],
        b"\x17\x03\x03\x00\x01x",
        *control[1:],
    ]


# ---------------------------------------------------------------------------
# refused requests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_a_request_the_node_refuses_leaves_the_connection_and_its_number(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    """402 for request 1 (the balance covers the deposit, not the
    reservation), then the same request again after a top-up: one
    connection, nothing held for the refusal."""
    detail = {"error": {"type": "insufficient_quota", "code": "insufficient_balance"}}
    ledger = _Ledger(
        monkeypatch,
        deposit=DEP,
        fees=FEES,
        reserve_errors={1: conf.BillingError("Insufficient balance", 402, detail)},
    )
    with _Conn(monkeypatch, lambda n: _settled(n, http), http) as conn:
        conn.open()
        conn.send(_request(**_FEE))
        assert conn.recv1() == {
            "type": "request_refused",
            "n": 1,
            "status": 402,
            "reason": "Insufficient balance",
            "detail": detail,
        }
        assert conn.holder["requests"] == []  # never reached the sidecar
        conn.send(_request(**_FEE))
        receipt = conn.until("receipt")["receipt"]
    assert receipt["n"] == 1 and receipt["deposit_released_msat"] == DEP
    # The session fee was still due: the refused request settled nothing.
    assert [r["session_fee_due"] for r in ledger.reserved] == [True, True]
    assert receipt["cost_msats"] == 2012
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve",
        "reserve",
        "settle:rel-2",
        "release:dep",
    ]
    assert [r["n"] for r in conn.holder["requests"]] == [1]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"model": ""}, "request.model"),
        ({"max_tokens": 0}, "request.max_tokens"),
        ({"max_tokens": 4097}, "request.max_tokens"),
        ({"max_tokens": "12"}, "request.max_tokens"),
        ({"max_tokens": True}, "request.max_tokens"),
        ({"body_len": 0}, "request.body_len"),
        ({"body_len": (8 << 20) + 1}, "request.body_len"),
        ({"kind": "video"}, "request.kind"),
        ({"offer_sig": "xyz"}, "request.offer_sig"),
        ({"auth": 5}, "request.auth"),
        ({"suffix": 5}, "request.suffix"),
        ({"session_fee_msat": -1}, "session_fee_msat"),
        ({"variant": "webp"}, "no variant or count"),
    ],
)
def test_a_malformed_request_is_refused_before_any_reservation(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    change: dict[str, Any],
    reason: str,
) -> None:
    ledger = _Ledger(monkeypatch)
    with _Conn(monkeypatch, http="2") as conn:
        conn.open()
        conn.send(_request(**change))
        refused = conn.recv1()
        assert refused["type"] == "request_refused" and refused["status"] == 400
        assert reason in refused["reason"]
        conn.send(_request())  # the connection is as it was
        assert conn.recv1()["type"] == "request_ok"
    assert ledger.calls == ["check", "reserve", "release:rel-1"]


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_a_request_the_sidecar_refuses_is_released_in_full(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    """The node reserved, the sidecar does not serve the terms: the whole
    reservation goes back (no abandon fee) and the number is free again."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    refusal = {
        "type": "request_refused",
        "n": 1,
        "status": 400,
        "reason": "model not allowed",
    }
    answers = iter([[refusal], _settled(1, http)])
    with _Conn(monkeypatch, lambda n: next(answers), http) as conn:
        conn.open()
        conn.send(_request(**_FEE))
        assert conn.recv1() == refusal
        assert cws._live_sids[SID].billing is None
        conn.send(_request(**_FEE))
        receipt = conn.until("receipt")["receipt"]
    assert receipt["n"] == 1 and receipt["cost_msats"] == 2012
    assert [r["n"] for r in conn.holder["requests"]] == [1, 1]
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve",
        "release:rel-1",
        "reserve",
        "settle:rel-2",
        "release:dep",
    ]


def test_a_request_refusal_for_another_request_is_dropped(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    stray = {"type": "request_refused", "n": 7, "status": 400, "reason": "x"}
    with _Conn(monkeypatch, lambda n: [stray, _request_ok(n, "2")], "2") as conn:
        conn.open()
        conn.send(_request())
        assert conn.recv1()["type"] == "request_ok"
    assert ledger.calls == ["check", "reserve", "release:rel-1"]


# ---------------------------------------------------------------------------
# protocol errors end the connection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 2, "1", None, True])
def test_a_wrong_request_number_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], n: Any
) -> None:
    ledger = _Ledger(monkeypatch, deposit=DEP)
    with _Conn(monkeypatch, http="2") as conn:
        conn.open()
        conn.send(_request(n))
        err = conn.recv1()
        assert err["type"] == "error" and err["status"] == 400
        assert "request.n must be 1" in err["reason"]
        # Only a protocol error: the deposit is charged (§12.10 row 12).
        assert _deposit(err) == ("charged", "unused", DEP)
        assert conn.closed()
    assert ledger.calls == ["check", "deposit", "charge:dep"]


def test_a_second_request_while_one_is_in_flight_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch, fees=FEES)
    with _Conn(monkeypatch, http="2") as conn:
        conn.open()
        conn.send(_request(**_FEE))
        assert conn.recv1()["type"] == "request_ok"
        conn.send(_request(2, **_FEE))
        err = conn.recv1()
        assert err["type"] == "error" and err["status"] == 409
        assert "already in flight" in err["reason"]
    # The open request was accepted and dropped: its abandon fee.
    assert ledger.calls == ["check", "reserve", "fee:rel-1:5000"]


def test_a_framing_2_request_before_the_head_is_written_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch, deposit=DEP)
    with _Conn(monkeypatch, http="2", on_setup=[_ready("2")]) as conn:
        conn.send(_setup("2"))
        assert conn.recv1()["type"] == "ready"
        conn.send(_request())
        err = conn.recv1()
        assert err["type"] == "error" and "head_written" in err["reason"]
        assert _deposit(err) == ("charged", "unused", DEP)
    assert ledger.calls == ["check", "deposit", "charge:dep"]
    assert conn.holder["requests"] == []


def test_a_framing_11_request_comes_before_the_head(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """In framing "1.1" the same moment is the right one: the head names the
    request."""
    ledger = _Ledger(monkeypatch)
    with _Conn(monkeypatch, http="1.1") as conn:
        conn.open()
        conn.send(_request())
        assert conn.recv1()["type"] == "request_ok"
    assert ledger.calls == ["check", "reserve", "release:rel-1"]


def test_duplicate_setup_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    with _Conn(monkeypatch, http="2") as conn:
        conn.open()
        conn.send(_setup("2"))
        err = conn.recv1()
        assert err["type"] == "error" and err["reason"] == "duplicate setup"
    assert ledger.calls == ["check"]


def test_frames_for_another_request_are_dropped(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """The node bills and signs only for the request in flight."""
    ledger = _Ledger(monkeypatch)
    stale = [_released(1), _attest(1, "2"), _usage(1, "2")]
    answers = {1: _settled(1, "2"), 2: [_request_ok(2, "2"), *stale]}
    with _Conn(monkeypatch, lambda n: answers[n], "2") as conn:
        conn.open()
        conn.send(_request())
        conn.until("receipt")
        conn.send(_request(2))
        assert conn.recv1()["type"] == "request_ok"
        state = cws._live_sids[SID]
        for _ in range(50):  # let the stale frames arrive
            time.sleep(0.01)
        assert state.body_released is False and state.attestation_hash == ""
    assert ledger.calls == [
        "check",
        "reserve",
        "settle:rel-1",
        "reserve",
        "release:rel-2",
    ]


def test_an_oversized_request_frame_ends_the_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    with _Conn(monkeypatch, http="2") as conn:
        conn.open()
        conn.send(_request(auth="x" * (cws.MAX_SETUP_TEXT + 1)))
        err = conn.recv1()
        assert err["type"] == "error" and err["status"] == 413
    assert ledger.calls == ["check"]


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def test_setup_for_a_framing_the_sidecar_does_not_serve_is_refused(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """E.g. while the sidecar has withdrawn framing "2"."""
    side = {**SIDE_OFFER, "http": ["1.1"]}
    ledger = _Ledger(monkeypatch, side=side)
    holder, err = _refused_setup(monkeypatch, _setup("2"))
    assert err["status"] == 400 and 'framing "2"' in err["reason"]
    assert ledger.calls == [] and "setup" not in holder


def test_setup_without_funds_never_reaches_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    refusal = conf.BillingError("Insufficient balance", 402, {"error": "x"})
    ledger = _Ledger(monkeypatch, credential_error=refusal, deposit=DEP)
    holder, err = _refused_setup(monkeypatch, _setup("2"))
    assert err["status"] == 402 and err["detail"] == {"error": "x"}
    # §12.10 row 1: never reserved, nothing to report.
    assert "deposit" not in err
    assert ledger.calls == ["check"] and ledger.deposits == []
    assert "setup" not in holder


def test_a_setup_that_cannot_pay_the_deposit_never_reaches_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    detail = {
        "error": {"type": "connection_deposit_unfunded", "connection_deposit_msat": DEP}
    }
    refusal = conf.BillingError("the balance does not cover", 402, detail)
    ledger = _Ledger(monkeypatch, deposit_error=refusal)
    holder, err = _refused_setup(monkeypatch, _setup("1.1"))
    assert err["status"] == 402 and err["detail"] == detail
    assert "deposit" not in err
    assert ledger.calls == ["check"] and "setup" not in holder


# ---------------------------------------------------------------------------
# the connection deposit (§12.10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_a_connection_dropped_without_a_request_loses_its_deposit(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    """Row 12: the client left."""
    ledger = _Ledger(monkeypatch, deposit=DEP)
    with _Conn(monkeypatch, http=http) as conn:
        assert conn.open()["deposit_msat"] == DEP
    assert ledger.calls == ["check", "deposit", "charge:dep"]
    assert ledger.deposits == [("sk-client-key", SIG)]


def test_a_client_that_leaves_while_the_sidecar_is_sending_is_not_a_sidecar_failure(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """The sidecar's pump ends because the client's socket did: that must not
    read as "the sidecar's socket ended" (which releases the deposit)."""
    ledger = _Ledger(monkeypatch, deposit=DEP)

    class _GoneClient(_IdleClient):
        async def send_text(self, text: str) -> None:
            raise RuntimeError("the client is gone")

    async def run() -> cws.SessionState:
        state = cws.SessionState()
        state.framing = "2"
        state.sid = SID
        state.h2_deposit = await cws.reserve_deposit("sk-client-key", SIG)
        sidecar = _QueueSidecar([_ready("2"), _HEAD], [])
        loop = asyncio.get_running_loop()
        await cws._run_h2_session(_GoneClient(), sidecar, state, loop.time())  # type: ignore[arg-type]
        return state

    state = asyncio.run(run())
    assert state.h2_fault is None and state.h2_sidecar_gone is False
    assert ledger.calls == ["deposit", "charge:dep"]


@pytest.mark.parametrize(
    "last,reason",
    [
        # Row 3: the provider unreachable, h2 not selected, "2" withdrawn.
        ({"type": "error", "reason": "x", "fault": "upstream"}, "provider_unreachable"),
        (
            {
                "type": "error",
                "reason": "x",
                "fault": "upstream",
                "code": "h2_withdrawn",
            },
            "provider_unreachable",
        ),
        # Row 4: the sidecar failed.
        (
            {"type": "error", "reason": "internal error", "fault": "node"},
            "sidecar_failure",
        ),
        # Row 5: the provider closed the open, idle connection.
        ({"type": "closed", "reason": "upstream_closed"}, "provider_closed"),
    ],
)
def test_the_deposit_is_released_when_the_client_could_not_have_used_the_connection(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    last: dict[str, Any],
    reason: str,
) -> None:
    _secret, pub_hex = node_identity
    ledger = _Ledger(monkeypatch, deposit=DEP)
    with _Conn(monkeypatch, http="2", on_setup=[_ready("2"), _HEAD, last]) as conn:
        conn.open()
        frame = conn.recv1()
        assert conn.closed()
    # The sidecar's last frame, with the deposit's outcome signed.
    assert {k: frame[k] for k in last} == last
    assert _deposit(frame) == ("released", reason, DEP)
    dep = frame["deposit"]
    assert dep["sid"] == SID and dep["cost_msats"] == 0
    assert dep["balance_msats"] == 97000 and isinstance(dep["time"], int)
    assert set(dep) == {
        "sid",
        "msat",
        "outcome",
        "reason",
        "cost_msats",
        "balance_msats",
        "time",
    }
    assert json.loads(frame["deposit_json"]) == dep
    assert _verify_schnorr(frame["deposit_json"], frame["deposit_sig"], pub_hex)
    assert frame["sig_scheme"] == "schnorr-secp256k1"
    assert ledger.calls == ["check", "deposit", "release:dep"]


@pytest.mark.parametrize(
    "last,reason",
    [
        # Row 12: a failed handshake or π_NK, a protocol error, the provider
        # closing during the handshake (no fault named by the sidecar).
        ({"type": "error", "reason": "proof pi_nk failed"}, "unused"),
        ({"type": "error", "reason": "x", "code": "h2_table_size"}, "unused"),
        ({"type": "closed", "reason": "idle"}, "unused"),
        ({"type": "closed", "reason": "max_lifetime"}, "unused"),
    ],
)
def test_the_deposit_is_charged_when_an_unused_connection_ends(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    last: dict[str, Any],
    reason: str,
) -> None:
    ledger = _Ledger(monkeypatch, deposit=DEP)
    with _Conn(monkeypatch, http="2", on_setup=[_ready("2"), _HEAD, last]) as conn:
        conn.open()
        frame = conn.recv1()
    assert frame["type"] == last["type"]
    assert _deposit(frame) == ("charged", reason, DEP)
    assert frame["deposit"]["cost_msats"] == DEP
    assert ledger.calls == ["check", "deposit", "charge:dep"]


def test_the_deposit_is_released_when_the_sidecar_refuses_the_setup(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Row 2: no ``ready``, so nothing was garbled."""
    ledger = _Ledger(monkeypatch, deposit=DEP)
    refusal = {"type": "error", "reason": "too many sessions"}
    with _Conn(monkeypatch, http="2", on_setup=[refusal]) as conn:
        conn.send(_setup("2"))
        frame = conn.recv1()
    assert frame["type"] == "error"
    assert _deposit(frame) == ("released", "not_started", DEP)
    assert frame["deposit"]["sid"] is None
    assert ledger.calls == ["check", "deposit", "release:dep"]


def test_the_deposit_is_released_when_the_sidecar_socket_ends_unexplained(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Row 4: neither ``error`` nor ``closed``."""
    ledger = _Ledger(monkeypatch, deposit=DEP)
    on_setup = [_ready("2"), _HEAD, {"hang_up": True}]
    with _Conn(monkeypatch, http="2", on_setup=on_setup) as conn:
        conn.open()
        frame = conn.recv1()
    assert frame["type"] == "error" and frame["status"] == 502
    assert frame["fault"] == "node"
    assert _deposit(frame) == ("released", "sidecar_failure", DEP)
    assert ledger.calls == ["check", "deposit", "release:dep"]


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_deposit_when_the_node_fails_a_request(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    """Row 6: a request refused with a 5xx is the node's own failure; the
    connection stays, and if it then ends unused the deposit goes back."""
    ledger = _Ledger(
        monkeypatch, deposit=DEP, reserve_errors={1: RuntimeError("db down")}
    )
    with _Conn(monkeypatch, http=http) as conn:
        conn.open()
        conn.send(_request())
        refused = conn.recv1()
        assert refused["type"] == "request_refused" and refused["status"] == 500
        assert refused["reason"] == "reservation failed"
    assert ledger.calls == ["check", "deposit", "reserve", "release:dep"]


def test_deposit_when_only_refused_requests_were_sent(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Row 12: a 4xx refusal by itself costs nothing, and the deposit follows
    how the connection ends."""
    ledger = _Ledger(
        monkeypatch,
        deposit=DEP,
        reserve_errors={1: conf.BillingError("model 'x' is not served confidentially")},
    )
    with _Conn(monkeypatch, http="2") as conn:
        conn.open()
        conn.send(_request(model="x"))
        assert conn.recv1()["type"] == "request_refused"
    assert ledger.calls == ["check", "deposit", "reserve", "charge:dep"]


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_deposit_when_a_settlement_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    """Row 6: the node's last frame says what failed and returns the deposit."""
    ledger = _Ledger(monkeypatch, deposit=DEP, settle_error=RuntimeError("db down"))
    monkeypatch.setattr(cws, "SETTLE_RETRY_DELAYS", (0, 0, 0))
    frames = [f for f in _settled(1, http) if f["type"] != "closed"]
    with _Conn(monkeypatch, lambda n: frames, http) as conn:
        conn.open()
        conn.send(_request())
        err = conn.until("error")
        assert conn.closed()
    assert err["status"] == 500 and err["fault"] == "node"
    assert err["detail"]["error"]["outcome"] == "charged_reservation"
    assert _deposit(err) == ("released", "node_failure", DEP)
    assert ledger.calls[-2:] == ["charge:rel-1", "release:dep"]


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_deposit_when_the_first_request_was_received_and_never_settled(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    """Row 7: the provider had the request; its reservation is charged, so
    the deposit is not charged on top."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    frames = [_request_ok(1, http), _released(1)]
    with _Conn(monkeypatch, lambda n: frames, http) as conn:
        conn.open()
        conn.send(_request(**_FEE))
        conn.until("body_released")
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve",
        "release:dep",
        "charge:rel-1",
    ]


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_deposit_when_the_provider_refused_the_first_request_early(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    """Row 8: the provider answered and closed before the body was complete:
    the request keeps the session fee, the deposit goes back."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    frames = [
        _request_ok(1, http),
        {"type": "upstream_closed_early", "n": 1, "response_bytes": 517},
        {"type": "error", "reason": "upstream closed the connection"},
    ]
    with _Conn(monkeypatch, lambda n: frames, http) as conn:
        conn.open()
        conn.send(_request(**_FEE))
        err = conn.until("error")
    assert _deposit(err) == ("released", "provider_refused", DEP)
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve",
        "release:dep",
        "fee:rel-1:2000",
    ]


@pytest.mark.parametrize("http", ["1.1", "2"])
@pytest.mark.parametrize(
    "ending",
    [
        [],  # the client leaves
        [{"type": "error", "reason": "proof pi_win failed"}],  # a failed proof
    ],
)
def test_deposit_when_the_first_request_is_abandoned(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    http: str,
    ending: list[dict[str, Any]],
) -> None:
    """Row 9: accepted, never released. The deposit is charged and the
    reservation released in full: the abandon fee is not kept on top."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    frames = [_request_ok(1, http), *ending]
    with _Conn(monkeypatch, lambda n: frames, http) as conn:
        conn.open()
        conn.send(_request(**_FEE))
        conn.until("request_ok")
        if ending:
            assert _deposit(conn.recv1()) == ("charged", "request_abandoned", DEP)
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve",
        "charge:dep",
        "release:rel-1",
    ]


@pytest.mark.parametrize("fault", ["upstream", "node"])
def test_deposit_when_the_provider_is_unreachable_with_request_1_open(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], fault: str
) -> None:
    """Framing "1.1" dials the provider with request 1 already accepted: a
    failure there is rows 3 and 4, and the request costs nothing either."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    err = {"type": "error", "reason": "connecting timed out", "fault": fault}
    with _Conn(monkeypatch, lambda n: [_request_ok(1), err], "1.1") as conn:
        conn.open()
        conn.send(_request(**_FEE))
        frame = conn.until("error")
    reason = "provider_unreachable" if fault == "upstream" else "sidecar_failure"
    assert _deposit(frame) == ("released", reason, DEP)
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve",
        "release:dep",
        "release:rel-1",
    ]


def test_an_abandoned_later_request_keeps_the_abandon_fee(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Once the deposit was released, a dropped request costs the abandon
    fee, and the connection's end says nothing about a deposit."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    answers = {
        1: _settled(1, "2"),
        2: [_request_ok(2, "2"), {"type": "error", "reason": "proof pi_win failed"}],
    }
    with _Conn(monkeypatch, lambda n: answers[n], "2") as conn:
        conn.open()
        conn.send(_request(**_FEE))
        conn.until("receipt")
        conn.send(_request(2, **_FEE))
        err = conn.until("error")
    assert "deposit" not in err
    assert ledger.calls[-2:] == ["reserve", "fee:rel-2:5000"]
    assert "charge:dep" not in ledger.calls


def test_a_released_later_request_without_a_settlement_is_charged(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    refusal = {"type": "error", "reason": "the disclosed records start before"}
    answers = {
        1: _settled(1, "2"),
        2: [_request_ok(2, "2"), _released(2), refusal],
    }
    with _Conn(monkeypatch, lambda n: answers[n], "2") as conn:
        conn.open()
        conn.send(_request(**_FEE))
        conn.until("receipt")
        conn.send(_request(2, **_FEE))
        assert "deposit" not in conn.until("error")
    assert ledger.calls[-2:] == ["reserve", "charge:rel-2"]


@pytest.mark.parametrize("http", ["1.1", "2"])
@pytest.mark.parametrize(
    "status,charge",
    [(429, "fee:rel-1:2000"), (404, "fee:rel-1:5000"), (500, "charge:rel-1")],
)
def test_an_error_head_is_a_settlement_and_returns_the_deposit(
    monkeypatch: pytest.MonkeyPatch,
    node_identity: tuple[str, str],
    http: str,
    status: int,
    charge: str,
) -> None:
    """Any request the provider received counts: the deposit goes back with
    the error receipt, and the connection closes ``error_settled``."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    frames = _settled(1, http, _error_usage(status, 1, http))
    with _Conn(monkeypatch, lambda n: frames, http) as conn:
        conn.open()
        conn.send(_request(**_FEE))
        receipt = conn.until("receipt")["receipt"]
        assert conn.recv1() == {"type": "closed", "reason": "error_settled"}
    assert receipt["deposit_released_msat"] == DEP
    assert receipt["usage"]["error_status"] == status
    assert ledger.calls == ["check", "deposit", "reserve", charge, "release:dep"]


def test_an_error_head_after_the_first_settlement_owes_no_session_fee(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """429 on request 2: "less the session fee if still due" is nothing."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES)
    answers = {1: _settled(1, "2"), 2: _settled(2, "2", _error_usage(429, 2, "2"))}
    with _Conn(monkeypatch, lambda n: answers[n], "2") as conn:
        conn.open()
        conn.send(_request(**_FEE))
        conn.until("receipt")
        conn.send(_request(2, **_FEE))
        receipt = conn.until("receipt")["receipt"]
    assert receipt["cost_msats"] == 0 and "session_fee_msat" not in receipt
    assert ledger.calls[-2:] == ["reserve", "release:rel-2"]


def test_no_deposit_when_the_offer_asks_for_none(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch, deposit=0)
    last = _closed("upstream_closed")
    with _Conn(monkeypatch, http="2", on_setup=[_ready("2"), _HEAD, last]) as conn:
        assert conn.open()["deposit_msat"] == 0
        assert conn.recv1() == last  # nothing to report
    assert ledger.calls == ["check"]


# ---------------------------------------------------------------------------
# billing: the node's ordinary reservation path
# ---------------------------------------------------------------------------


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Key:
    hashed_key = "abc"


async def test_check_credential_needs_a_known_offer_and_funds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routstr.auth as auth
    import routstr.core.db as db

    seen: dict[str, Any] = {}

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
    assert unknown.value.detail["error"]["type"] == "offer_expired"

    # The floor: the largest of 1 msat, the minimum request and the fees.
    conf._remember_offer(SIG, conf.OfferTerms({}))
    assert await conf.check_credential("sk-x", SIG) == "abc"
    assert seen == {"bearer": "sk-x", "min_cost": 3}
    conf._remember_offer(SIG, conf.OfferTerms({}, session_fee_msat=50))
    await conf.check_credential("sk-x", SIG)
    assert seen["min_cost"] == 50
    conf._remember_offer(
        SIG, conf.OfferTerms({}, session_fee_msat=50, abandon_fee_msat=80)
    )
    await conf.check_credential("sk-x", SIG)
    assert seen["min_cost"] == 80

    from fastapi import HTTPException

    async def broke(bearer: str, session: Any, min_cost: int = 0) -> Any:
        raise HTTPException(
            status_code=402, detail={"error": {"message": "no funds", "type": "t"}}
        )

    monkeypatch.setattr(auth, "validate_bearer_key", broke)
    with pytest.raises(conf.BillingError) as refused:
        await conf.check_credential("sk-x", SIG)
    assert refused.value.status == 402
    assert refused.value.detail == {"error": {"message": "no funds", "type": "t"}}
    # The reason a client shows is the message, not a Python repr.
    assert str(refused.value) == "no funds"


async def test_reserve_deposit_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    import routstr.auth as auth
    import routstr.core.db as db

    seen: dict[str, Any] = {}

    async def validate(bearer: str, session: Any, min_cost: int = 0) -> Any:
        seen.update(bearer=bearer, min_cost=min_cost)
        return _Key()

    async def broke(key: Any, cost: int, session: Any) -> Any:
        raise HTTPException(
            status_code=402,
            detail={"error": {"code": "insufficient_balance", "message": "no"}},
        )

    monkeypatch.setattr(db, "create_session", lambda: _Session())
    monkeypatch.setattr(auth, "validate_bearer_key", validate)
    monkeypatch.setattr(auth, "pay_for_request", broke)

    with pytest.raises(conf.BillingError) as unknown:
        await conf.reserve_deposit("sk-x", SIG)
    assert unknown.value.status == 409
    # No deposit in the offer: nothing is reserved.
    conf._remember_offer(SIG, conf.OfferTerms({}, connection_deposit_msat=0))
    assert await conf.reserve_deposit("sk-x", SIG) is None and seen == {}
    # Too little balance: the node's 402 body, marked as the deposit's.
    conf._remember_offer(SIG, conf.OfferTerms({}, connection_deposit_msat=700))
    with pytest.raises(conf.BillingError) as unfunded:
        await conf.reserve_deposit("sk-x", SIG)
    assert seen == {"bearer": "sk-x", "min_cost": 700}
    assert unfunded.value.status == 402
    error = unfunded.value.detail["error"]
    assert error["type"] == "connection_deposit_unfunded"
    assert error["code"] == "insufficient_balance"
    assert error["connection_deposit_msat"] == 700


def _deposit_sig(deposit: int) -> str:
    sig = "de" * 64
    conf._remember_offer(sig, conf.OfferTerms({}, connection_deposit_msat=deposit))
    return sig


def _keyed_validate(billing_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``validate_bearer_key`` with the ``min_cost`` the setup passes."""
    import routstr.auth as auth
    from routstr.core.db import ApiKey

    async def validate(bearer: str, session: Any, min_cost: int = 0) -> Any:
        return await session.get(ApiKey, bearer)

    monkeypatch.setattr(auth, "validate_bearer_key", validate)


async def test_the_deposit_is_an_ordinary_reservation(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    """Reserved through ``pay_for_request``; released, it costs nothing;
    charged, it costs exactly its amount, booked under a name of its own."""
    _keyed_validate(billing_db, monkeypatch)
    sig = _deposit_sig(7000)
    deposit = await conf.reserve_deposit("hk", sig)
    assert deposit is not None and deposit.kind == "deposit"
    assert deposit.reserved_msats == 7000 and deposit.candidate is None
    assert deposit.model_id == conf.DEPOSIT_MODEL_ID
    key = await billing_db.key()
    assert (key.balance, key.reserved_balance) == (3_000_000, 7000)

    assert await conf.release(deposit) == 3_000_000
    key = await billing_db.key()
    assert (key.balance, key.reserved_balance, key.total_spent) == (3_000_000, 0, 0)

    deposit = await conf.reserve_deposit("hk", sig)
    assert deposit is not None
    assert await conf.charge_reservation(deposit) == 2_993_000
    key = await billing_db.key()
    assert (key.balance, key.reserved_balance, key.total_spent) == (2_993_000, 0, 7000)


async def test_a_deposit_and_a_request_are_held_together(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any, fee_settings: None
) -> None:
    """The deposit stays reserved while request 1 is reserved on top; the
    request's reservation is its usage bound plus the larger fee."""
    _keyed_validate(billing_db, monkeypatch)
    deposit = await conf.reserve_deposit("hk", _deposit_sig(7000))
    assert deposit is not None
    billing = await conf.reserve("hk", "ctx-model", 8, _current_sig(), 2000, 10000)
    # 100k context × 12 + 8 × 120 msat at the surcharged rates, + the abandon fee.
    assert billing.reserved_msats == 1_200_960 + 10_000
    key = await billing_db.key()
    assert key.reserved_balance == 7000 + billing.reserved_msats
    usage = {
        "model": "ctx-model",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 8},
    }
    assert (await conf.settle(billing, usage))["cost_msats"] == 12_960 + 2000
    await conf.release(deposit)
    key = await billing_db.key()
    assert (key.reserved_balance, key.total_spent) == (0, 14_960)


async def test_the_session_fee_is_charged_once_per_connection(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any, fee_settings: None
) -> None:
    """A request after the connection's first settlement reserves the same
    (the abandon fee is still at stake) and owes no session fee."""
    usage = {
        "model": "ctx-model",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 8},
    }
    first = await conf.reserve("hk", "ctx-model", 8, _current_sig(), 2000, 10000)
    later = await conf.reserve(
        "hk", "ctx-model", 8, _current_sig(), 2000, 10000, session_fee_due=False
    )
    assert first.reserved_msats == later.reserved_msats
    assert (first.session_fee_msat, later.session_fee_msat) == (2000, 0)
    assert first.offer_session_fee_msat == later.offer_session_fee_msat == 2000
    assert later.abandon_fee_msat == 10000
    assert (await conf.settle(first, usage))["cost_msats"] == 12_960 + 2000
    assert (await conf.settle(later, usage))["cost_msats"] == 12_960
    # The client still has to accept the offer's fees in every request.
    with pytest.raises(conf.BillingError) as err:
        await conf.reserve("hk", "ctx-model", 8, _current_sig(), session_fee_due=False)
    assert err.value.detail["error"]["type"] == "session_fee_not_accepted"
    # A provider refusal after the first settlement keeps nothing.
    later = await conf.reserve(
        "hk", "ctx-model", 8, _current_sig(), 2000, 10000, session_fee_due=False
    )
    assert cws._fee(later, "session_fee_msat") == 0
    assert cws._fee(later, "abandon_fee_msat") == 10000
    assert await cws._release_keeping_fee(later, "session_fee_msat") == (
        0,
        (await billing_db.key()).balance,
    )
    assert (await billing_db.key()).reserved_balance == 0


# ---------------------------------------------------------------------------
# limits and time
# ---------------------------------------------------------------------------


def test_connection_counts_as_unused_until_its_first_request_is_released(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    frames = [_request_ok(1, "2"), {"sleep": 0.3}, _released(1)]
    with _Conn(monkeypatch, lambda n: frames, "2") as conn:
        conn.open()
        # ``head_written`` alone does not make the connection "used".
        assert cws._unused_by_ip == {"testclient": 1}
        conn.send(_request())
        conn.until("request_ok")
        assert cws._unused_by_ip == {"testclient": 1}
        conn.until("body_released")
        assert cws._unused_by_ip == {}
    assert cws._unused_by_ip == {} and cws._active_sessions == 0


def test_unused_limit_refuses_further_connections(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(settings, "confidential_max_unused_per_ip", 1)
    with _Conn(monkeypatch, http="2") as first:
        first.open()
        with _Conn(monkeypatch, http="2") as second:
            err = second.recv1()
            assert err["type"] == "error" and err["status"] == 429
    assert ledger.calls == ["check"]


async def _run_idle(
    monkeypatch: pytest.MonkeyPatch,
    frames: list[dict[str, Any]],
    deposit: int,
    settled: int = 0,
) -> tuple[_Ledger, Any, Any, cws.SessionState]:
    """Run a connection whose client sends nothing until the node ends it."""
    ledger = _Ledger(monkeypatch, deposit=deposit)
    sidecar = _QueueSidecar(frames, [])
    state = cws.SessionState()
    state.framing = "2"
    state.sid = SID
    state.h2_idle_timeout = 0.2
    state.h2_settled = settled
    if deposit:
        state.h2_deposit = await cws.reserve_deposit("sk-client-key", SIG)
    client = _IdleClient()
    started = asyncio.get_running_loop().time()
    await cws._run_h2_session(client, sidecar, state, started)  # type: ignore[arg-type]
    return ledger, client, sidecar, state


async def test_idle_connection_is_closed_after_the_idle_timeout(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """The node closes a connection with no request in flight for
    ``idle_timeout_s``. After its first settlement nothing is held."""
    ledger, client, sidecar, _ = await _run_idle(
        monkeypatch, [_ready("2"), _HEAD], 0, settled=1
    )
    assert sidecar.closed
    assert client.sent[-1] == {"type": "closed", "reason": "idle"}
    assert ledger.calls == []


async def test_idle_connection_that_never_carried_a_request_loses_its_deposit(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """Row 10."""
    _secret, pub_hex = node_identity
    ledger, client, sidecar, state = await _run_idle(
        monkeypatch, [_ready("2"), _HEAD], DEP
    )
    assert sidecar.closed and state.h2_deposit is None
    assert ledger.calls == ["deposit", "charge:dep"]
    # The client is told in ``closed``, signed like a receipt.
    closed = client.sent[-1]
    assert closed["type"] == "closed" and closed["reason"] == "idle"
    assert _deposit(closed) == ("charged", "idle", DEP)
    record = closed["deposit"]
    assert record["cost_msats"] == DEP and record["sid"] == SID
    assert record["balance_msats"] == 91000
    assert json.loads(closed["deposit_json"]) == record
    assert _verify_schnorr(closed["deposit_json"], closed["deposit_sig"], pub_hex)


async def test_the_idle_time_never_exceeds_the_sidecars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()
    st = cws.SessionState()
    st.h2_idle_since = now
    st.h2_idle_timeout, st.h2_idle_secs = 120.0, 300.0
    assert cws._h2_deadline(st, now - 50) == now + 120
    st.h2_idle_secs = 45.0
    assert cws._h2_deadline(st, now - 50) == now + 45


async def test_request_time_limit_charges_a_released_request(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """A request the provider has and that is never disclosed is charged at
    its own time limit; the limit runs from the release."""
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(cws, "POST_RELEASE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(cws, "SETTLE_GRACE_S", 0.0)
    answer = [_request_ok(1, "2"), _released(1)]
    state = cws.SessionState()
    state.framing = "2"
    state.sid = SID

    class _Client(_IdleClient):
        def __init__(self) -> None:
            super().__init__()
            self.first = True

        async def receive(self) -> dict[str, Any]:
            if self.first:
                self.first = False
                # After ``head_written`` reached the node.
                await asyncio.sleep(0.05)
                return {"type": "websocket.receive", "text": json.dumps(_request(1))}
            await asyncio.Event().wait()
            return {}

    class _Sidecar(_QueueSidecar):
        async def send(self, data: Any) -> None:
            # The request arrived: answer it.
            for f in answer:
                self.queue.put_nowait(json.dumps(f))

    sidecar = _Sidecar([_ready("2"), _HEAD], [])
    client = _Client()
    await cws._run_h2_session(
        client,  # type: ignore[arg-type]
        sidecar,
        state,
        asyncio.get_running_loop().time(),
    )
    assert any(m.get("status") == 408 for m in client.sent)
    assert ledger.calls == ["reserve", "charge:rel-1"]


def test_the_sidecars_orderly_end_is_the_last_frame(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """``max_requests`` (also ``max_lifetime``, ``max_bytes``): after the
    settlement that reached the limit."""
    ledger = _Ledger(monkeypatch, deposit=DEP)
    frames = [*_settled(1, "2"), _closed("max_requests")]
    with _Conn(monkeypatch, lambda n: frames, "2") as conn:
        conn.open()
        conn.send(_request())
        conn.until("receipt")
        assert conn.recv1() == {"type": "closed", "reason": "max_requests"}
        assert conn.closed()
    assert ledger.calls[-1] == "release:dep"


def test_the_proof_channels_outlive_the_offers_connection_lifetime(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    side = {**SIDE_OFFER, "h2": {**H2, "max_lifetime_s": 7200}}
    _Ledger(monkeypatch, side=side)
    with _Conn(monkeypatch, http="2") as conn:
        conn.open()
        assert cws._live_sids[SID].h2_max_lifetime == 7200.0
        assert cws._live_sids[SID].h2_idle_secs == 300.0


# ---------------------------------------------------------------------------
# image requests on a connection
# ---------------------------------------------------------------------------


def test_a_chat_request_and_an_image_request_share_a_connection(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES, images=True)
    answers = {
        1: _settled(1, "2"),
        2: _settled(2, "2", _flat_usage(2, "2", 2)),
        3: _settled(3, "2"),
    }
    with _Conn(monkeypatch, lambda n: answers[n], "2") as conn:
        conn.open()
        conn.send(_request(**_FEE))
        chat = conn.until("receipt")["receipt"]
        conn.send(_image_request(2, count=2, **_FEE))
        image = conn.until("receipt")["receipt"]
        conn.send(_request(3, **_FEE))
        again = conn.until("receipt")["receipt"]
    assert chat["deposit_released_msat"] == DEP and chat["cost_msats"] == 2012
    assert "kind" not in chat and chat["rates"] == RATES
    # The image: the flat price, no session fee (it was charged with request 1).
    assert image["kind"] == "images" and image["n"] == 2 and image["http"] == "2"
    assert image["cost_msats"] == 2 * IMAGE_PRICE
    assert image["reserved_msats"] == 2 * IMAGE_PRICE + 5000
    assert "deposit_released_msat" not in image and "session_fee_msat" not in image
    assert again["cost_msats"] == 12
    assert conn.holder["requests"][1] == {
        "type": "request",
        "n": 2,
        "kind": "images",
        "model": "z-image-turbo",
        "count": 2,
        "body_len": 150,
        "suffix": IMAGE_SUFFIX,
    }
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve",
        "settle:rel-1",
        "release:dep",
        "reserve_image",
        "flat:rel-2",
        "reserve",
        "settle:rel-3",
    ]
    assert [r["session_fee_due"] for r in ledger.reserved] == [True, False, False]


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_an_image_request_as_the_first_request_returns_the_deposit(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES, images=True)
    frames = _settled(1, http, _flat_usage(1, http))
    with _Conn(monkeypatch, lambda n: frames, http) as conn:
        conn.open()
        conn.send(_image_request(**_FEE))
        receipt = conn.until("receipt")["receipt"]
    # The price plus the session fee, due with the first settlement.
    assert receipt["cost_msats"] == IMAGE_PRICE + 2000
    assert receipt["session_fee_msat"] == 2000
    assert receipt["deposit_released_msat"] == DEP
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve_image",
        "flat:rel-1",
        "release:dep",
    ]


def test_an_abandoned_image_request(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    """As a chat request: before any settlement the deposit is charged and
    the reservation released; afterwards the abandon fee is kept."""
    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES, images=True)
    fail = {"type": "error", "reason": "proof pi_win failed"}
    with _Conn(monkeypatch, lambda n: [_request_ok(n, "2"), fail], "2") as conn:
        conn.open()
        conn.send(_image_request(**_FEE))
        assert _deposit(conn.until("error")) == ("charged", "request_abandoned", DEP)
    assert ledger.calls == [
        "check",
        "deposit",
        "reserve_image",
        "charge:dep",
        "release:rel-1",
    ]

    ledger = _Ledger(monkeypatch, deposit=DEP, fees=FEES, images=True)
    answers = {1: _settled(1, "2"), 2: [_request_ok(2, "2"), fail]}
    with _Conn(monkeypatch, lambda n: answers[n], "2") as conn:
        conn.open()
        conn.send(_request(**_FEE))
        conn.until("receipt")
        conn.send(_image_request(2, **_FEE))
        conn.until("error")
    assert ledger.calls[-2:] == ["reserve_image", "fee:rel-2:5000"]


async def test_flat_settlement_owes_the_session_fee_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    charged: list[int] = []

    async def charge(billing: Any, cost: int) -> int:
        charged.append(cost)
        return 0

    monkeypatch.setattr(conf, "_charge", charge)

    def billing(fee: int) -> conf.Billing:
        return conf.Billing(
            "k",
            "img",
            None,
            2 * 10_000 + 5000,
            None,
            {},
            session_fee_msat=fee,
            offer_session_fee_msat=2000,
            abandon_fee_msat=5000,
            kind="images",
            count=2,
            msat_per_image=10_000,
        )

    assert (await conf.settle_flat(billing(2000)))["cost_msats"] == 22_000
    assert (await conf.settle_flat(billing(0)))["cost_msats"] == 20_000
    assert charged == [22_000, 20_000]
