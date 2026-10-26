"""Long-context pricing tiers (OpenRouter ``pricing.overrides``).

Anthropic and Gemini charge more per token once a prompt passes a threshold
(Haiku 5.5: 5x above 100k, Sonnet 4.5: 2x above 200k). OpenRouter publishes
these as ``pricing.overrides`` entries keyed on ``min_prompt_tokens``; litellm
as ``*_above_<N>k_tokens`` keys. Billing a long prompt on the base rate
under-charges it, so the tier must survive parsing, fee/sats scaling, the
reservation ceiling, and the token-priced settlement.
"""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from routstr.core.settings import settings
from routstr.payment.cost_calculation import CostData, calculate_cost
from routstr.payment.models import (
    Architecture,
    Model,
    Pricing,
    TopProvider,
    _calculate_usd_max_costs,
    _update_model_sats_pricing,
    has_usable_pricing,
    litellm_pricing_tiers,
)

# Verbatim shape of OpenRouter's anthropic/claude-haiku-5.5 pricing.
HAIKU_OPENROUTER_PRICING = {
    "prompt": "0.0000001",
    "completion": "0.0000005",
    "web_search": "0.01",
    "input_cache_read": "0.00000001",
    "input_cache_write": "0.000000125",
    "input_cache_write_1h": "0.0000002",
    "overrides": [
        {
            "min_prompt_tokens": 100000,
            "prompt": "0.0000005",
            "completion": "0.0000025",
            "input_cache_read": "0.00000005",
            "input_cache_write": "0.000000625",
            "input_cache_write_1h": "0.000001",
        }
    ],
}


def _model(pricing: Pricing, context_length: int = 200000) -> Model:
    return Model(
        id="anthropic/claude-haiku-5.5",
        name="Claude Haiku 5.5",
        created=0,
        description="",
        context_length=context_length,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="Claude",
            instruct_type=None,
        ),
        pricing=pricing,
        top_provider=TopProvider(context_length=context_length),
    )


@pytest.fixture(autouse=True)
def _pricing_env(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[misc]
    monkeypatch.setattr(settings, "fixed_pricing", False)
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=1e-4):
        yield


def test_openrouter_overrides_parse_into_tiers() -> None:
    pricing = Pricing.parse_obj(HAIKU_OPENROUTER_PRICING)

    assert len(pricing.overrides) == 1
    tier = pricing.overrides[0]
    assert tier.min_prompt_tokens == 100000
    assert tier.prompt == pytest.approx(5e-7)
    assert tier.completion == pytest.approx(2.5e-6)


def test_time_window_overrides_are_not_applied_as_tiers() -> None:
    """DeepSeek's off-peak entries carry ``utc_*`` conditions, not a threshold."""
    pricing = Pricing.parse_obj(
        {
            "prompt": "0.00000015",
            "completion": "0.0000006",
            "overrides": [
                {"utc_days": ["saturday"], "prompt": "0.0000003"},
                {"min_prompt_tokens": 1000, "utc_start": 100, "prompt": "0.0000003"},
                {"prompt": "0.0000003"},
                "garbage",
            ],
        }
    )

    assert pricing.overrides == []


def test_for_prompt_tokens_threshold_is_exclusive() -> None:
    pricing = Pricing.parse_obj(HAIKU_OPENROUTER_PRICING)

    assert pricing.for_prompt_tokens(100000).prompt == pytest.approx(1e-7)
    assert pricing.for_prompt_tokens(100001).prompt == pytest.approx(5e-7)
    # Rates the tier does not list keep their base value.
    assert pricing.for_prompt_tokens(100001).web_search == pytest.approx(0.01)


def test_highest_matching_tier_wins() -> None:
    pricing = Pricing(
        prompt=1.0,
        completion=1.0,
        overrides=[
            {"min_prompt_tokens": 200000, "prompt": 3.0},
            {"min_prompt_tokens": 100000, "prompt": 2.0},
        ],
    )

    assert pricing.for_prompt_tokens(150000).prompt == 2.0
    assert pricing.for_prompt_tokens(250000).prompt == 3.0


def test_scaling_carries_tiers_through_fee_and_sats_conversion() -> None:
    pricing = Pricing.parse_obj(HAIKU_OPENROUTER_PRICING).scaled(2.0)
    model = _update_model_sats_pricing(_model(pricing), sats_to_usd=1e-4)

    assert model.sats_pricing is not None
    tier = model.sats_pricing.overrides[0]
    assert tier.min_prompt_tokens == 100000
    assert tier.prompt == pytest.approx(5e-7 * 2 / 1e-4)


def test_max_cost_reserves_at_the_long_context_rate() -> None:
    """A full 200k window is billed on the 5x tier, so the ceiling must be too."""
    model = _model(Pricing.parse_obj(HAIKU_OPENROUTER_PRICING))

    max_prompt, _, max_cost = _calculate_usd_max_costs(model)

    assert max_prompt == pytest.approx(200000 * 5e-7)
    assert max_cost == pytest.approx(200000 * 2.5e-6)


def test_unusable_tier_rate_disqualifies_pricing() -> None:
    pricing = Pricing(
        prompt=1e-7,
        completion=5e-7,
        overrides=[{"min_prompt_tokens": 100000, "prompt": float("nan")}],
    )

    assert not has_usable_pricing(pricing)


def _sats_model() -> Model:
    usd = Pricing.parse_obj(HAIKU_OPENROUTER_PRICING)
    return _update_model_sats_pricing(_model(usd), sats_to_usd=1e-4)


@pytest.mark.asyncio
async def test_short_prompt_bills_base_rate() -> None:
    # 1e-7 USD / 1e-4 USD-per-sat = 1 msat per input token; output 5 msats.
    response = {
        "model": "anthropic/claude-haiku-5.5",
        "usage": {"prompt_tokens": 50000, "completion_tokens": 1000},
    }

    result = await calculate_cost(response, 10**9, model_obj=_sats_model())

    assert isinstance(result, CostData)
    assert result.total_msats == 50000 + 5000


@pytest.mark.asyncio
async def test_long_prompt_bills_long_context_tier() -> None:
    response = {
        "model": "anthropic/claude-haiku-5.5",
        "usage": {"prompt_tokens": 150000, "completion_tokens": 1000},
    }

    result = await calculate_cost(response, 10**9, model_obj=_sats_model())

    assert isinstance(result, CostData)
    # 5x on every rate: 5 msats per input token, 25 per output token.
    assert result.total_msats == 150000 * 5 + 1000 * 25


@pytest.mark.asyncio
async def test_cached_tokens_count_toward_the_threshold() -> None:
    """Anthropic counts cache reads/writes in the prompt size the tier keys on."""
    response = {
        "model": "anthropic/claude-haiku-5.5",
        "usage": {
            "prompt_tokens": 120000,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 110000},
        },
    }

    result = await calculate_cost(response, 10**9, model_obj=_sats_model())

    assert isinstance(result, CostData)
    # 10k uncached at 5 msats + 110k cache reads at the tier's 0.5 msats.
    assert result.total_msats == 10000 * 5 + 110000 * 0.5


def test_litellm_above_k_keys_become_tiers() -> None:
    info = {
        "input_cost_per_token": 3e-6,
        "output_cost_per_token": 1.5e-5,
        "input_cost_per_token_above_200k_tokens": 6e-6,
        "output_cost_per_token_above_200k_tokens": 2.25e-5,
        "cache_read_input_token_cost_above_200k_tokens": 6e-7,
        # Batch and cache-duration variants are other service tiers.
        "input_cost_per_token_above_200k_tokens_batches": 3e-6,
        "cache_creation_input_token_cost_above_1hr": 6e-6,
    }

    tiers = litellm_pricing_tiers(info)

    assert len(tiers) == 1
    assert tiers[0].min_prompt_tokens == 200000
    assert tiers[0].prompt == 6e-6
    assert tiers[0].completion == 2.25e-5
    assert tiers[0].input_cache_read == 6e-7
    assert tiers[0].input_cache_write is None


@pytest.mark.asyncio
async def test_litellm_fallback_bills_long_context_tier() -> None:
    info = {
        "input_cost_per_token": 3e-6,
        "output_cost_per_token": 1.5e-5,
        "input_cost_per_token_above_200k_tokens": 6e-6,
        "output_cost_per_token_above_200k_tokens": 2.25e-5,
    }
    response = {
        "model": "claude-sonnet-4-5",
        "usage": {"prompt_tokens": 250000, "completion_tokens": 1000},
    }

    with (
        patch("routstr.proxy.get_model_instance", return_value=None),
        patch("routstr.payment.models.litellm_cost_entry", return_value=info),
    ):
        result = await calculate_cost(response, 10**9, model_obj=None, provider_fee=1.0)

    assert isinstance(result, CostData)
    # 6e-6 USD / 1e-4 = 60 msats per input token, 225 per output token.
    assert result.total_msats == 250000 * 60 + 1000 * 225
