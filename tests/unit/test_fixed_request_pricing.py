"""Fixed pricing without per-1k rates settles at the flat per-request fee.

The reservation holds ``fixed_cost_per_request``; settlement must charge that
same flat amount whatever the token counts or upstream-reported USD cost,
while still reporting the usage it saw.
"""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from routstr.core.settings import settings
from routstr.payment.cost_calculation import CostData, MaxCostData, calculate_cost
from routstr.payment.models import Architecture, Model, Pricing

FLAT_SATS = 7
FLAT_MSATS = FLAT_SATS * 1000


@pytest.fixture(autouse=True)
def flat_request_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_cost_per_request", FLAT_SATS)
    monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", 0)
    monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", 0)


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> None:  # type: ignore[misc]
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-4):
        yield


def _priced_model() -> Model:
    """A model whose own token prices would bill far above the flat fee."""
    return Model(
        id="token-priced-model",
        name="token-priced-model",
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="test",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.01, completion=0.01),
        sats_pricing=Pricing(prompt=0.01, completion=0.01),
        per_request_limits=None,
        top_provider=None,
    )


def _assert_flat(result: object) -> CostData:
    assert isinstance(result, CostData)
    assert not isinstance(result, MaxCostData)
    assert result.total_msats == FLAT_MSATS
    assert result.input_msats + result.output_msats == result.total_msats
    assert result.pricing_source == "fixed_request"
    return result


@pytest.mark.asyncio
async def test_small_usage_settles_at_flat_fee() -> None:
    response = {
        "model": "token-priced-model",
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }

    result = _assert_flat(
        await calculate_cost(response, FLAT_MSATS, model_obj=_priced_model())
    )

    assert result.input_tokens == 3
    assert result.output_tokens == 2


@pytest.mark.asyncio
async def test_usage_above_hold_settles_at_flat_fee() -> None:
    response = {
        "model": "token-priced-model",
        "usage": {"prompt_tokens": 3, "completion_tokens": 100_000},
    }

    result = _assert_flat(
        await calculate_cost(response, FLAT_MSATS, model_obj=_priced_model())
    )

    assert result.output_tokens == 100_000


@pytest.mark.asyncio
async def test_upstream_usd_cost_is_reported_not_billed() -> None:
    response = {
        "model": "token-priced-model",
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 10,
            "cost": 2.5,
            "cost_details": {"input_cost": 1.0, "output_cost": 1.5},
        },
    }

    result = _assert_flat(
        await calculate_cost(
            response, FLAT_MSATS, model_obj=_priced_model(), provider_fee=1.1
        )
    )

    assert result.upstream_usd == 2.5
    assert result.total_usd == pytest.approx(FLAT_SATS * 5.0e-4)


@pytest.mark.asyncio
async def test_cache_only_usage_settles_at_flat_fee() -> None:
    response = {
        "model": "token-priced-model",
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 500,
        },
    }

    result = _assert_flat(
        await calculate_cost(response, FLAT_MSATS, model_obj=_priced_model())
    )

    assert result.cache_read_input_tokens == 500


@pytest.mark.asyncio
async def test_estimated_usage_settles_at_flat_fee() -> None:
    response = {
        "model": "token-priced-model",
        "usage": {"input_tokens": 12, "output_tokens": 3, "estimated": True},
    }

    result = _assert_flat(
        await calculate_cost(response, FLAT_MSATS, model_obj=_priced_model())
    )

    assert result.input_source == result.output_source == "estimated"


@pytest.mark.asyncio
async def test_all_zero_usage_releases_the_hold() -> None:
    response = {
        "model": "token-priced-model",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.5},
    }

    result = await calculate_cost(response, FLAT_MSATS, model_obj=_priced_model())

    assert isinstance(result, CostData)
    assert result.total_msats == 0


@pytest.mark.asyncio
async def test_missing_usage_releases_the_hold() -> None:
    response = {"model": "token-priced-model"}

    result = await calculate_cost(response, FLAT_MSATS, model_obj=_priced_model())

    assert isinstance(result, MaxCostData)
    assert result.total_msats == 0


@pytest.mark.asyncio
async def test_per_1k_override_still_bills_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", 2)
    monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", 3)
    response = {
        "model": "token-priced-model",
        "usage": {"prompt_tokens": 1000, "completion_tokens": 2000},
    }

    result = await calculate_cost(response, FLAT_MSATS, model_obj=_priced_model())

    assert isinstance(result, CostData)
    assert result.pricing_source == "fixed"
    assert result.total_msats == 2000 + 6000
