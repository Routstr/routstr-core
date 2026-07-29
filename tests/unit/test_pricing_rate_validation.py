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

    The "no token pricing configured" gate is a truthiness test, and ``NaN`` and
    negative floats are both truthy, so an unusable rate passes the guard that
    exists to catch it. It then reaches the integer conversion in the token math,
    which raises ``ValueError`` for ``NaN`` and ``OverflowError`` for ``inf`` —
    after the upstream response has already been served, where the streaming
    handlers swallow it and the request goes unbilled.
    """
    model = _model(Pricing(prompt=bad_rate, completion=1.0))

    cost = await calculate_cost(_usage_response(), max_cost=1234, model_obj=model)

    assert isinstance(cost, MaxCostData)
    assert cost.total_msats == 1234


@pytest.mark.parametrize(
    "junk", [float("inf"), float("nan"), "Infinity"], ids=["inf", "nan", "inf-string"]
)
@pytest.mark.asyncio
async def test_junk_cost_component_still_bills_the_reported_total(junk: Any) -> None:
    """A malformed component must not discard the upstream's real total cost.

    ``cost_details`` only splits the total across input and output; the total is
    the authoritative billed amount. A non-finite component poisons the
    proportional allocation (``inf / inf`` is ``NaN``), which raised out of the
    USD path and was swallowed by the broad handler around it — so the request
    silently fell through to token-estimated pricing and was billed at a small
    fraction of what the upstream actually charged.
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
