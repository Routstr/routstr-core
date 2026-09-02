"""Tests that the upstream's own USD figure survives the provider-fee multiply.

``_calculate_from_usd_cost`` multiplies the fee into the same local that holds
the upstream's reported cost, so by the time a ``CostData`` exists the raw
figure is gone and only the marked-up one remains. Dividing the total back out
does not recover it: when the request falls through to token pricing the total
is the node's own arithmetic, and dividing it compares that number to itself.

These tests cover ``upstream_usd`` — the pre-fee figure carried alongside the
billed one, and the discriminator for whether an upstream reported a cost at
all. They also cover the boundary it must not cross: the pair of numbers spells
out the node's margin, so it stays internal and is never serialised to a client.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from routstr.payment.cost_calculation import CostData, calculate_cost
from routstr.payment.models import Architecture, Model, Pricing


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> Iterator[None]:
    """Pin the exchange rate; these tests are about the USD figure, not the feed."""
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-5):
        yield


def _model() -> Model:
    return Model(
        id="m",
        name="m",
        created=0,
        description="d",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=1e-06, completion=2e-06),
        sats_pricing=Pricing(prompt=1e-06, completion=2e-06),
    )


def _response(usage: dict[str, Any]) -> dict[str, Any]:
    return {"model": "m", "usage": usage}


@pytest.mark.asyncio
async def test_reported_cost_is_kept_alongside_the_billed_one() -> None:
    """The billed total carries the fee; ``upstream_usd`` must not."""
    response = _response(
        {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.01}
    )

    cost = await calculate_cost(
        response, max_cost=999999, model_obj=_model(), provider_fee=1.05
    )

    assert isinstance(cost, CostData)
    assert cost.total_usd == pytest.approx(0.0105)
    assert cost.upstream_usd == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_token_priced_request_reports_no_upstream_cost() -> None:
    """Nothing was reported, so there is nothing to carry — not our own total."""
    response = _response({"prompt_tokens": 1000, "completion_tokens": 500})

    cost = await calculate_cost(
        response, max_cost=999999, model_obj=_model(), provider_fee=1.05
    )

    assert isinstance(cost, CostData)
    assert cost.total_msats > 0
    assert cost.upstream_usd == 0.0


@pytest.mark.asyncio
async def test_reported_cost_is_never_serialised_to_a_client() -> None:
    """Publishing it beside the billed total would spell out the node's margin."""
    response = _response(
        {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.01}
    )

    cost = await calculate_cost(
        response, max_cost=999999, model_obj=_model(), provider_fee=1.05
    )

    assert isinstance(cost, CostData)
    assert cost.upstream_usd == pytest.approx(0.01)
    assert "upstream_usd" not in cost.dict()
    assert "upstream_usd" not in cost.json()


@pytest.mark.asyncio
async def test_billed_total_is_reproducible_from_the_reported_cost() -> None:
    """Fee, rate and rounding must carry the reported figure to the billed one.

    The identity a report can restate: whatever the upstream said, times the
    provider fee, converted at the current rate and rounded up, is what the node
    charged. A figure chosen for its awkward remainder keeps the ceiling honest.
    """
    response = _response(
        {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 0.000123}
    )

    cost = await calculate_cost(
        response, max_cost=999999, model_obj=_model(), provider_fee=1.03
    )

    assert isinstance(cost, CostData)
    assert cost.total_msats == math.ceil(cost.upstream_usd * 1.03 / 5.0e-5 * 1000)
    assert cost.total_msats == 2534
