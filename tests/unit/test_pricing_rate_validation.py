"""Tests that an unusable rate never reaches the money math.

A billable rate is usable only when it is finite and non-negative. Prices reach
the node from upstream catalogs, an operator's admin edit, a legacy database row
and the BTC/USD feed, and each of those can deliver ``NaN``, ``±inf`` or a
negative — ``json.loads`` accepts the bare ``NaN``/``Infinity`` literals and
overflows ``1e999`` to ``inf``.

These tests cover the guards between such a value and a charge: the token-rate
gate that decides a model cannot be priced, the upstream-reported USD cost, the
exchange-rate feed, and the stored-row read path. They assert the node declines
to price the request rather than billing a nonsensical amount or raising after
the response has already been served.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from routstr.payment.cost_calculation import (
    CostData,
    MaxCostData,
    calculate_cost,
)
from routstr.payment.models import (
    Architecture,
    Model,
    Pricing,
)


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> Iterator[None]:
    """Pin the exchange rate; these tests are about the rates, not the feed."""
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-5):
        yield


def _architecture() -> Architecture:
    return Architecture(
        modality="text",
        input_modalities=["text"],
        output_modalities=["text"],
        tokenizer="unknown",
        instruct_type=None,
    )


def _model(sats_pricing: Pricing) -> Model:
    return Model(
        id="m",
        name="m",
        created=0,
        description="d",
        context_length=8192,
        architecture=_architecture(),
        pricing=Pricing(prompt=1e-06, completion=2e-06),
        sats_pricing=sats_pricing,
    )


def _usage_response() -> dict[str, Any]:
    return {"model": "m", "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}


@pytest.mark.parametrize(
    "bad_rate",
    [float("nan"), float("inf"), -5.0],
    ids=["nan", "inf", "negative"],
)
@pytest.mark.asyncio
async def test_unusable_token_rate_falls_back_to_max_cost(bad_rate: float) -> None:
    """An unusable configured rate must not be billed on.

    It reached the token math, which raises after the response was already
    served — where the streaming handlers swallow it and the request goes
    unbilled.
    """
    model = _model(Pricing(prompt=bad_rate, completion=1.0))

    cost = await calculate_cost(_usage_response(), max_cost=1234, model_obj=model)

    assert isinstance(cost, MaxCostData)
    assert cost.total_msats == 1234


@pytest.mark.parametrize(
    ("prompt", "completion", "expected_msats"),
    [(0.0, 0.0, 0), (0.0, 2e-06, 1), (1e-06, 0.0, 1)],
    ids=["free", "free-input", "free-output"],
)
@pytest.mark.asyncio
async def test_a_rate_of_zero_is_billed_as_free_not_as_missing(
    prompt: float, completion: float, expected_msats: int
) -> None:
    """Zero is a price, and the request must be billed on it.

    The gate that decides a model has no token pricing was a truthiness test, so
    a free rate read as an absent one and the request was charged the whole
    reservation instead — on a model priced at zero for that side, which is a
    price the catalog serves and the router routes.
    """
    model = _model(Pricing(prompt=prompt, completion=completion))

    cost = await calculate_cost(_usage_response(), max_cost=1234, model_obj=model)

    assert isinstance(cost, CostData)
    assert cost.total_msats == expected_msats


@pytest.mark.parametrize(
    "junk", [float("inf"), float("nan"), "Infinity"], ids=["inf", "nan", "inf-string"]
)
@pytest.mark.asyncio
async def test_junk_cost_component_still_bills_the_reported_total(junk: Any) -> None:
    """A malformed component must not discard the upstream's real total cost.

    ``cost_details`` only splits the total across input and output; the total is
    the authoritative billed amount. A non-finite component poisoned the split,
    and the request fell through to token estimation for a fraction of it.
    """
    model = _model(Pricing(prompt=1e-06, completion=2e-06))
    response = {
        "model": "m",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "cost": 0.01,
            "cost_details": {"input_cost": junk, "output_cost": 0.004},
        },
    }

    cost = await calculate_cost(response, max_cost=9999, model_obj=model)

    assert isinstance(cost, CostData)
    # $0.01 at 5.0e-5 USD/sat = 200 sats = 200_000 msats.
    assert cost.total_msats == 200000
    assert cost.total_usd == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_junk_cost_component_falls_back_to_its_alternate_field() -> None:
    """A malformed component must not shadow the field that would have replaced it.

    Each side of the split has two spellings and the second is a fallback for a
    missing first. ``inf`` and ``NaN`` are both truthy, so a malformed
    ``input_cost`` won that choice before anything checked whether it was a
    number, and the usable figure beside it was never read — the input side was
    then billed at nothing and the whole total landed on output.
    """
    model = _model(Pricing(prompt=1e-06, completion=2e-06))
    response = {
        "model": "m",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "cost": 0.01,
            "cost_details": {
                "input_cost": float("inf"),
                "upstream_inference_prompt_cost": 0.006,
                "output_cost": 0.004,
            },
        },
    }

    cost = await calculate_cost(response, max_cost=9999, model_obj=model)

    assert isinstance(cost, CostData)
    # $0.01 at 5.0e-5 USD/sat = 200_000 msats, split 0.006 : 0.004.
    assert cost.total_msats == 200000
    assert (cost.input_msats, cost.output_msats) == (120000, 80000)


@pytest.mark.asyncio
async def test_non_finite_reported_cost_is_not_a_cost() -> None:
    """An upstream-reported ``Infinity`` cost is junk, not an infinite charge.

    ``json.loads`` accepts the bare ``Infinity`` literal, so a compromised or
    buggy upstream can put one in ``usage.cost``. It must not be treated as a
    positive USD cost at all — the request falls through to the node's own token
    pricing instead.
    """
    model = _model(Pricing(prompt=1e-06, completion=2e-06))
    response = {
        "model": "m",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "cost": float("inf"),
        },
    }

    cost = await calculate_cost(response, max_cost=9999, model_obj=model)

    assert isinstance(cost, CostData)
    assert math.isfinite(cost.total_usd)
    assert cost.total_msats == 2


class _ExchangeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        # A quote given as an exception stands for a response body that never
        # produced one: an exchange answering with an HTML error page raises
        # out of `.json()` before any price is read.
        quote = next(iter(self._payload.values()))
        if isinstance(quote, BaseException):
            raise quote
        return self._payload


class _ExchangeClient:
    """Answers each exchange endpoint with a caller-supplied quote."""

    def __init__(self, quotes: dict[str, Any]) -> None:
        self._quotes = quotes

    async def get(self, url: str) -> _ExchangeResponse:
        if "kraken" in url:
            quote = self._quotes["kraken"]
            if isinstance(quote, BaseException):
                return _ExchangeResponse({"error": quote})
            return _ExchangeResponse({"result": {"XXBTZUSD": {"c": [quote]}}})
        if "coinbase" in url:
            return _ExchangeResponse({"data": {"amount": self._quotes["coinbase"]}})
        return _ExchangeResponse({"price": self._quotes["binance"]})


class _AsyncCtx:
    def __init__(self, client: _ExchangeClient) -> None:
        self._client = client

    async def __aenter__(self) -> _ExchangeClient:
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def refresh_price_with() -> Iterator[Any]:
    """Refresh the node's BTC/USD price from caller-supplied exchange quotes.

    Restores the module's cached price afterwards so one test cannot set the
    rate another one bills at.
    """
    import routstr.payment.price as price_module

    previous = (price_module.BTC_USD_PRICE, price_module.SATS_USD_PRICE)

    async def _run(quotes: dict[str, Any], last_good: float | None = None) -> None:
        price_module.BTC_USD_PRICE = last_good
        price_module.SATS_USD_PRICE = (
            None if last_good is None else last_good / 100_000_000
        )
        with patch.object(
            price_module.httpx,
            "AsyncClient",
            lambda *a, **k: _AsyncCtx(_ExchangeClient(quotes)),
        ):
            await price_module._update_prices()

    yield _run

    price_module.BTC_USD_PRICE, price_module.SATS_USD_PRICE = previous


@pytest.mark.parametrize(
    "bad_quote",
    ["0", "0.00000000", "-1", "NaN", "Infinity", "N/A"],
    ids=["zero", "zero-padded", "negative", "nan", "infinity", "non-numeric"],
)
@pytest.mark.asyncio
async def test_unusable_exchange_quote_does_not_set_the_node_price(
    bad_quote: str, refresh_price_with: Any
) -> None:
    """One exchange returning junk must not set the price the node bills at.

    The feed takes the ``min()`` of what it collects, so an unusable quote does
    not merely join the sample — it *wins*. The two healthy quotes must still
    price the node.
    """
    from routstr.payment.price import btc_usd_price

    await refresh_price_with(
        {"kraken": bad_quote, "coinbase": "100000.0", "binance": "100000.0"}
    )

    assert btc_usd_price() == pytest.approx(100000.0)


@pytest.mark.asyncio
async def test_boolean_exchange_quote_does_not_set_the_node_price(
    refresh_price_with: Any,
) -> None:
    """A boolean in the price field is a shape change, not a $1 bitcoin.

    ``float(True)`` is ``1.0``, which is finite and positive, so a payload whose
    price field turned into a boolean passes every numeric guard — and then
    *wins* the ``min()``, pricing the whole node at one dollar per bitcoin.
    """
    from routstr.payment.price import btc_usd_price

    await refresh_price_with(
        {"kraken": True, "coinbase": "100000.0", "binance": "100000.0"}
    )

    assert btc_usd_price() == pytest.approx(100000.0)


@pytest.mark.asyncio
async def test_an_underflowing_exchange_quote_does_not_set_the_node_price(
    refresh_price_with: Any,
) -> None:
    """A quote too small to survive the sats conversion is not a price.

    ``1e-320`` is positive, so it passes the guards and wins the ``min()``, but
    the node prices in sats and ``1e-320 / 100_000_000`` underflows to ``0.0``
    — a zero sats price divides by zero on every model's rate.
    """
    from routstr.payment.price import btc_usd_price

    await refresh_price_with(
        {"kraken": "1e-320", "coinbase": "100000.0", "binance": "100000.0"}
    )

    assert btc_usd_price() == pytest.approx(100000.0)


@pytest.mark.asyncio
async def test_an_unreadable_exchange_response_drops_only_that_quote(
    refresh_price_with: Any,
) -> None:
    """An exchange whose response never yields a quote costs one quote.

    The price is aggregated across three exchanges so that one of them having a
    bad day is survivable; unhandled, the raise aborted the whole aggregation.
    """
    from routstr.payment.price import btc_usd_price

    await refresh_price_with(
        {
            "kraken": ValueError("Expecting value: line 1 column 1 (char 0)"),
            "coinbase": "100000.0",
            "binance": "100000.0",
        }
    )

    assert btc_usd_price() == pytest.approx(100000.0)


@pytest.mark.asyncio
async def test_all_quotes_unusable_keeps_the_last_good_price(
    refresh_price_with: Any,
) -> None:
    """When every quote is junk the node keeps the last price it trusted.

    Adopting ``0`` or ``NaN`` because it was the only thing on offer would take
    out billing for every model at once; skipping the update degrades to a stale
    rate, which is the safe direction and what an unreachable exchange already
    does.
    """
    from routstr.payment.price import btc_usd_price

    await refresh_price_with(
        {"kraken": "0", "coinbase": "NaN", "binance": "-3"}, last_good=90000.0
    )

    assert btc_usd_price() == pytest.approx(90000.0)


# ---------------------------------------------------------------------------
# Catalog ingest — a malformed rate must never become a stored price
# ---------------------------------------------------------------------------


class _CatalogResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _CatalogClient:
    """Stands in for ``httpx.AsyncClient`` against the OpenRouter catalog."""

    def __init__(self, models: list[dict[str, Any]]) -> None:
        self._models = models

    async def __aenter__(self) -> "_CatalogClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, timeout: int | None = None) -> _CatalogResponse:
        if url.endswith("/embeddings/models"):
            return _CatalogResponse({"data": []})
        return _CatalogResponse({"data": self._models})


def _catalog_entry(model_id: str, pricing: dict[str, Any]) -> dict[str, Any]:
    return {"id": model_id, "name": model_id, "pricing": pricing}


def _patch_openrouter_catalog(models: list[dict[str, Any]]) -> Any:
    return patch(
        "routstr.payment.models.httpx.AsyncClient",
        lambda *args, **kwargs: _CatalogClient(models),
    )


@pytest.mark.parametrize(
    "bad_rate",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "inf", "negative-inf"],
)
@pytest.mark.asyncio
async def test_non_finite_catalog_rate_is_not_imported(bad_rate: float) -> None:
    """A non-finite rate in the upstream catalog is junk, not a price.

    ``json.loads`` accepts the bare ``NaN``/``Infinity`` literals and overflows
    ``1e999`` to ``inf``, so an upstream feed can deliver one. The import filter
    rejects a negative and a both-zero price, but every comparison with ``NaN``
    is False and ``inf`` reads as a large positive, so both sailed through and
    became a stored price the node would advertise and bill on.
    """
    with _patch_openrouter_catalog(
        [
            _catalog_entry("bad", {"prompt": bad_rate, "completion": "0.000002"}),
            _catalog_entry("good", {"prompt": "0.000001", "completion": "0.000002"}),
        ]
    ):
        from routstr.payment.models import async_fetch_openrouter_models

        models = await async_fetch_openrouter_models()

    assert [m["id"] for m in models] == ["good"]


@pytest.mark.asyncio
async def test_oversized_catalog_rate_does_not_empty_the_catalog() -> None:
    """An integer too large to be a float must cost one model, not all of them.

    ``float()`` raises ``OverflowError`` — not ``ValueError`` — for such a
    value, so the coercion guard in the import filter did not catch it and the
    exception unwound the whole fetch. The node then imported nothing at all
    from an upstream whose catalog was fine apart from one entry.
    """
    with _patch_openrouter_catalog(
        [
            _catalog_entry("bad", {"prompt": 10**400, "completion": 2}),
            _catalog_entry("good", {"prompt": "0.000001", "completion": "0.000002"}),
        ]
    ):
        from routstr.payment.models import async_fetch_openrouter_models

        models = await async_fetch_openrouter_models()

    assert [m["id"] for m in models] == ["good"]


@pytest.mark.asyncio
async def test_boolean_catalog_rate_is_not_imported() -> None:
    """A JSON ``true`` is a change of shape, not a price.

    Python coerces it to a finite, positive ``1.0`` — a dollar per token — so it
    passes every numeric guard and must be rejected before coercion.
    """
    with _patch_openrouter_catalog(
        [
            _catalog_entry("bad", {"prompt": True, "completion": "0.000002"}),
            _catalog_entry("good", {"prompt": "0.000001", "completion": "0.000002"}),
        ]
    ):
        from routstr.payment.models import async_fetch_openrouter_models

        models = await async_fetch_openrouter_models()

    assert [m["id"] for m in models] == ["good"]
