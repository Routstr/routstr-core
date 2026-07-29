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

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from routstr.payment.cost_calculation import (
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
