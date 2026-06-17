"""Tests for cache-aware pricing of cached input tokens.

Specifies two things:

1. ``backfill_cache_pricing`` — when the OpenRouter model feed omits cache
   rates (it does for most DeepSeek models and e.g. openai/gpt-4o), they are
   filled from litellm's bundled cost map instead of silently billing cache
   reads at the full input rate. Existing OpenRouter values are never
   overwritten, and provider fees apply to backfilled rates like any other.
2. ``calculate_cost`` — cached tokens are billed at the cache rates from the
   model's sats_pricing; the full input rate remains only as the documented
   last resort when no cache rate could be resolved anywhere.
"""

import os
from unittest.mock import Mock, patch

import litellm
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
    backfill_cache_pricing,
)
from routstr.upstream import GenericUpstreamProvider


def _make_model(model_id: str, pricing: Pricing) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=64000,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="Other",
            instruct_type=None,
        ),
        pricing=pricing,
    )


# ============================================================================
# backfill_cache_pricing — litellm as fallback source for missing cache rates
# ============================================================================


def test_backfill_deepseek_cache_read_from_litellm() -> None:
    """deepseek/deepseek-chat has no input_cache_read on OpenRouter; litellm
    knows the real rate (10x cheaper than input)."""
    pricing = Pricing(prompt=2.8e-07, completion=4.2e-07)

    result = backfill_cache_pricing("deepseek/deepseek-chat", pricing)

    expected = litellm.model_cost["deepseek/deepseek-chat"][
        "cache_read_input_token_cost"
    ]
    assert result.input_cache_read == expected
    assert result.input_cache_read < pricing.prompt  # sanity: it's a discount


def test_backfill_strips_vendor_prefix_for_litellm_lookup() -> None:
    """OpenRouter ids are vendor-prefixed (openai/gpt-4o); litellm keys most
    non-DeepSeek models without the prefix (gpt-4o)."""
    pricing = Pricing(prompt=2.5e-06, completion=1e-05)

    result = backfill_cache_pricing("openai/gpt-4o", pricing)

    expected = litellm.model_cost["gpt-4o"]["cache_read_input_token_cost"]
    assert result.input_cache_read == expected


def test_backfill_fills_cache_write_rate() -> None:
    """Anthropic cache writes cost more than input (1.25x); billing them at
    the input rate undercharges. litellm carries the write rate."""
    pricing = Pricing(prompt=3e-06, completion=1.5e-05)

    result = backfill_cache_pricing("anthropic/claude-sonnet-4-5", pricing)

    expected = litellm.model_cost["claude-sonnet-4-5"][
        "cache_creation_input_token_cost"
    ]
    assert result.input_cache_write == expected
    assert result.input_cache_write > pricing.prompt  # sanity: write premium


def test_backfill_never_overwrites_openrouter_rates() -> None:
    """When OpenRouter provides a cache rate, it is authoritative."""
    pricing = Pricing(
        prompt=2.1e-07, completion=7.9e-07, input_cache_read=1.3e-07
    )

    result = backfill_cache_pricing("deepseek/deepseek-chat", pricing)

    assert result.input_cache_read == 1.3e-07


def test_backfill_unknown_model_unchanged() -> None:
    """Models litellm doesn't know stay untouched (last-resort fallback to
    the input rate happens later, at billing time)."""
    pricing = Pricing(prompt=1e-06, completion=2e-06)

    result = backfill_cache_pricing("artificial-dumbness/dumb-1", pricing)

    assert result.input_cache_read == 0.0
    assert result.input_cache_write == 0.0


def test_provider_fee_applies_to_backfilled_cache_rates() -> None:
    """Backfill happens before the provider fee, so cache rates carry the
    same markup as every other price component."""
    provider = GenericUpstreamProvider(
        base_url="http://upstream.example", provider_fee=2.0
    )
    model = _make_model(
        "deepseek/deepseek-chat", Pricing(prompt=2.8e-07, completion=4.2e-07)
    )

    adjusted = provider._apply_provider_fee_to_model(model)

    litellm_read = litellm.model_cost["deepseek/deepseek-chat"][
        "cache_read_input_token_cost"
    ]
    assert adjusted.pricing.input_cache_read == pytest.approx(litellm_read * 2.0)
    assert adjusted.pricing.prompt == pytest.approx(2.8e-07 * 2.0)


# ============================================================================
# calculate_cost — cached tokens billed at cache rates
# ============================================================================


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> None:  # type: ignore[misc]
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-5):
        yield


@pytest.fixture
def model_pricing(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Model-based pricing: 1 msat per input token, 2 per output token,
    0.1 per cache-read token, 1.25 per cache-write token."""
    monkeypatch.setattr(settings, "fixed_pricing", False)
    model = Mock()
    model.sats_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        input_cache_read=0.0001,
        input_cache_write=0.00125,
    )
    return model


@pytest.mark.asyncio
async def test_deepseek_cache_hits_billed_at_cache_rate(model_pricing: Mock) -> None:
    """The reported overcharge scenario: a 10k-token prompt with 90% cache
    hits costs 2900 msats at honest rates, not the 11000 msats that billing
    every prompt token at the full input rate would charge."""
    response = {
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 10000,
            "completion_tokens": 500,
            "prompt_cache_hit_tokens": 9000,
            "prompt_cache_miss_tokens": 1000,
        },
    }

    with patch("routstr.proxy.get_model_instance", return_value=model_pricing):
        result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    # 1000 input @ 1 msat + 9000 cache reads @ 0.1 msat + 500 output @ 2 msat.
    # Client-visible input_msats includes cache-read input cost for display,
    # while cache_read_msats keeps the detailed breakdown.
    assert result.input_msats == 1900
    assert result.cache_read_msats == 900
    assert result.output_msats == 1000
    assert result.total_msats == 2900


@pytest.mark.asyncio
async def test_anthropic_cache_write_billed_at_write_rate(model_pricing: Mock) -> None:
    """Cache writes carry their premium rate (1.25x input here), instead of
    being silently billed at the plain input rate."""
    response = {
        "model": "claude-sonnet-4-5",
        "usage": {
            "input_tokens": 300,
            "output_tokens": 100,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 2000,
        },
    }

    with patch("routstr.proxy.get_model_instance", return_value=model_pricing):
        result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    # 300 @ 1 + 500 @ 0.1 + 2000 @ 1.25 + 100 @ 2
    assert result.cache_read_msats == 50
    assert result.cache_creation_msats == 2500
    assert result.total_msats == 3050


@pytest.mark.asyncio
async def test_missing_cache_rate_falls_back_to_input_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented last resort: when no cache rate could be resolved anywhere
    (OpenRouter and litellm both silent), cache reads bill at the input rate —
    never cheaper, never free."""
    monkeypatch.setattr(settings, "fixed_pricing", False)
    model = Mock()
    model.sats_pricing = Pricing(prompt=0.001, completion=0.002)

    response = {
        "model": "dumb-1",
        "usage": {
            "prompt_tokens": 10000,
            "completion_tokens": 500,
            "prompt_cache_hit_tokens": 9000,
            "prompt_cache_miss_tokens": 1000,
        },
    }

    with patch("routstr.proxy.get_model_instance", return_value=model):
        result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    # 1000 @ 1 + 9000 @ 1 (fallback) + 500 @ 2
    assert result.total_msats == 11000
