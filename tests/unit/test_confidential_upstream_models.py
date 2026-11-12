"""Models whose public id is not the provider's (an ``org/`` prefix).

The node offers such a model under its public id; the chat suffix pins the
provider's own id, which the signed offer lists in ``upstream_models``. The
request names the public id, the sidecar is sent the provider's, the receipt
names both, and a provider that reports its own id is no mismatch.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import routstr.confidential as conf
import routstr.confidential_ws as cws

# ruff: noqa: F811  (the imported fixtures are requested by name)
from .test_confidential import (  # noqa: F401
    RATES,
    _attest,
    _Billing,
    _ctx_model,
    _current_sig,
    _fresh_module_state,
    _Ledger,
    _request,
    _request_ok,
    _run_request,
    _Up,
    _usage,
    billing_db,
    node_identity,
)

PUBLIC = "kimi-k3.1-pro"
PROVIDER = "moonshotai/kimi-k3.1-pro"


async def _side() -> dict[str, Any]:
    return {
        "upstream_host": "api.numes.ai",
        "head_template": "x",
        "v": 3,
        "twopc": "split-public-1",
    }


async def test_offer_lists_the_provider_id_only_where_it_differs(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    up = _Up("https://api.numes.ai/api/v1")
    cands = {
        PUBLIC: conf.Candidate(SimpleNamespace(id=PROVIDER), up, PROVIDER),
        "same": conf.Candidate(SimpleNamespace(id="same"), up, "same"),
    }
    monkeypatch.setattr(conf, "cached_sidecar_offer", _side)
    monkeypatch.setattr(conf, "offered_models", lambda s: cands)
    monkeypatch.setattr(conf, "price_entry", lambda mid, c: dict(RATES))
    offer = await conf.build_offer()
    assert set(offer["price_list"]) == {PUBLIC, "same"}
    assert offer["upstream_models"] == {PUBLIC: PROVIDER}
    terms = conf.signed_offer_terms(offer["sig"])
    assert terms is not None and conf.pinned_model(terms, PUBLIC) == PROVIDER
    # /v1/models lists the public id.
    assert PUBLIC in await conf.models_fields()

    # A provider whose ids are the public ones (Venice): no such field.
    monkeypatch.setattr(conf, "offered_models", lambda s: {"same": cands["same"]})
    assert "upstream_models" not in await conf.build_offer()


async def test_reserve_pins_the_named_offers_provider_id(
    monkeypatch: pytest.MonkeyPatch, billing_db: Any
) -> None:
    cand = conf.Candidate(_ctx_model(), _Up("https://api.venice.ai/api/v1"), PROVIDER)
    monkeypatch.setattr(conf, "offered_models", lambda s: {"ctx-model": cand})
    sig = _current_sig()
    entry = conf.signed_offer_terms(sig).price_list["ctx-model"]  # type: ignore[union-attr]
    # The current offer (built like the endpoint's) lists the provider id.
    conf._remember_offer(
        "ab" * 64,
        conf.OfferTerms({"ctx-model": entry}, upstream_models={"ctx-model": PROVIDER}),
    )
    billing = await conf.reserve("hk", "ctx-model", 8, "ab" * 64)
    assert billing.model_id == "ctx-model"
    assert billing.pinned_model == PROVIDER
    await conf.release(billing)
    # An offer that pinned another id (the provider's id changed since):
    # the client must refetch, as for an expired offer.
    conf._remember_offer("cd" * 64, conf.OfferTerms({"ctx-model": entry}))
    with pytest.raises(conf.BillingError) as exc:
        await conf.reserve("hk", "ctx-model", 8, "cd" * 64)
    assert exc.value.status == 409


class _PrefixedBilling(_Billing):
    def __init__(self, release_id: str = "rel-1") -> None:
        super().__init__(release_id)
        self.model_id = PUBLIC
        self.upstream_model = PROVIDER

    @property
    def pinned_model(self) -> str:
        return self.upstream_model


@pytest.mark.parametrize("http", ["1.1", "2"])
def test_sidecar_gets_the_provider_id_and_the_receipt_names_both(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str], http: str
) -> None:
    ledger = _Ledger(monkeypatch)
    warned: list[Any] = []

    async def reserve(*args: Any, **kwargs: Any) -> Any:
        ledger.calls.append("reserve")
        ledger.reserved.append({"model": args[1]})
        return _PrefixedBilling()

    monkeypatch.setattr(cws, "reserve", reserve)
    monkeypatch.setattr(cws.logger, "warning", lambda msg, *a, **k: warned.append(msg))
    usage = _usage(http=http, model=PROVIDER)
    usage["usage"]["model"] = PROVIDER
    holder, got = _run_request(
        monkeypatch,
        [_request_ok(http=http), _attest(http=http), usage],
        3,
        http=http,
        request=_request(model=PUBLIC),
    )
    # The client named the public id; the node reserved under it and sent
    # the sidecar the provider's (the suffix's pinned model).
    assert ledger.reserved[0]["model"] == PUBLIC
    assert holder["requests"][0]["model"] == PROVIDER
    receipt = got[-1]["receipt"]
    assert receipt["model"] == PUBLIC
    assert receipt["upstream_model"] == PROVIDER
    assert receipt["rates"] == RATES
    # The provider reporting its own id is no mismatch.
    assert not any("another model" in str(m) for m in warned)


def test_receipt_has_no_upstream_model_when_the_ids_agree(
    monkeypatch: pytest.MonkeyPatch, node_identity: tuple[str, str]
) -> None:
    _Ledger(monkeypatch)
    holder, got = _run_request(monkeypatch, [_request_ok(), _attest(), _usage()], 3)
    assert holder["requests"][0]["model"] == "test-model"
    assert "upstream_model" not in got[-1]["receipt"]
