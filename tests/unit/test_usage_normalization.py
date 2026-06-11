"""Tests for vendor-agnostic usage normalization.

Specifies the seam that keeps vendor usage dialects out of generic billing
code: a canonical ``NormalizedUsage`` shape produced by
``routstr.payment.usage.normalize_usage`` (union parser for the known,
non-colliding dialects) and exposed as an overridable
``BaseUpstreamProvider.normalize_usage`` hook for vendors whose fields would
genuinely conflict. ``calculate_cost`` accepts a pre-normalized usage and then
needs no vendor knowledge of its own.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from routstr.core.settings import settings
from routstr.payment.cost_calculation import CostData, calculate_cost
from routstr.payment.usage import NormalizedUsage, normalize_usage
from routstr.upstream import BaseUpstreamProvider, GenericUpstreamProvider


@pytest.fixture(autouse=True)
def mock_fixed_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", 0.001)
    monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", 0.001)


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> None:  # type: ignore[misc]
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-5):
        yield


# ============================================================================
# The union parser: one canonical shape for all known dialects
# ============================================================================


@pytest.mark.parametrize(
    "usage,expected",
    [
        # OpenAI: cached_tokens included in prompt_tokens → subtracted
        (
            {
                "prompt_tokens": 2000,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
            NormalizedUsage(
                input_tokens=1200,
                output_tokens=100,
                cache_read_tokens=800,
                cache_write_tokens=0,
            ),
        ),
        # DeepSeek: hit/miss fields, prompt_tokens = hit + miss → hit subtracted
        (
            {
                "prompt_tokens": 10000,
                "completion_tokens": 500,
                "prompt_cache_hit_tokens": 9000,
                "prompt_cache_miss_tokens": 1000,
            },
            NormalizedUsage(
                input_tokens=1000,
                output_tokens=500,
                cache_read_tokens=9000,
                cache_write_tokens=0,
            ),
        ),
        # Anthropic: cache fields additive, input_tokens NOT reduced
        (
            {
                "input_tokens": 300,
                "output_tokens": 100,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 2000,
            },
            NormalizedUsage(
                input_tokens=300,
                output_tokens=100,
                cache_read_tokens=500,
                cache_write_tokens=2000,
            ),
        ),
        # Plain OpenAI without caching
        (
            {"prompt_tokens": 100, "completion_tokens": 50},
            NormalizedUsage(input_tokens=100, output_tokens=50),
        ),
    ],
)
def test_normalize_usage_dialects(usage: dict, expected: NormalizedUsage) -> None:
    """Each known vendor dialect maps onto the same canonical shape."""
    assert normalize_usage(usage) == expected


def test_normalize_usage_absent_usage() -> None:
    """Missing/invalid usage yields None so callers can bill at max cost."""
    assert normalize_usage(None) is None
    assert normalize_usage("not a dict") is None  # type: ignore[arg-type]


def test_normalize_usage_never_negative() -> None:
    """Buggy upstreams reporting more cached than prompt tokens clamp to 0."""
    result = normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_cache_hit_tokens": 150,
        }
    )
    assert result is not None
    assert result.input_tokens == 0
    assert result.cache_read_tokens == 150


# ============================================================================
# The provider hook: default delegates to the union parser, subclasses
# override for vendors whose fields genuinely conflict
# ============================================================================


class ArtificialDumbnessProvider(BaseUpstreamProvider):
    """Fictional vendor whose usage dialect collides with nothing we know."""

    provider_type = "artificial-dumbness"

    def normalize_usage(self, usage_data: object) -> NormalizedUsage | None:
        if not isinstance(usage_data, dict):
            return None
        return NormalizedUsage(
            input_tokens=usage_data.get("dumb_tokens_in", 0),
            output_tokens=usage_data.get("dumb_tokens_out", 0),
            cache_read_tokens=usage_data.get("dumb_tokens_reused", 0),
            cache_write_tokens=0,
        )


def test_base_provider_hook_delegates_to_union_parser() -> None:
    """Without an override, the provider hook equals the module parser, so
    DeepSeek-dialect upstreams work through GenericUpstreamProvider with no
    subclass."""
    provider = GenericUpstreamProvider(base_url="http://upstream.example")
    usage = {
        "prompt_tokens": 10000,
        "completion_tokens": 500,
        "prompt_cache_hit_tokens": 9000,
        "prompt_cache_miss_tokens": 1000,
    }
    assert provider.normalize_usage(usage) == normalize_usage(usage)


@pytest.mark.asyncio
async def test_provider_override_is_honored_by_calculate_cost() -> None:
    """The escape hatch: a vendor-specific override feeds calculate_cost
    through the `usage` parameter, and generic billing needs no knowledge of
    the vendor's field names."""
    provider = ArtificialDumbnessProvider(base_url="http://ad.example", api_key="k")
    response = {
        "model": "dumb-1",
        "usage": {
            "dumb_tokens_in": 700,
            "dumb_tokens_out": 60,
            "dumb_tokens_reused": 4300,
        },
    }

    result = await calculate_cost(
        response,
        max_cost=100000,
        session=AsyncMock(),
        usage=provider.normalize_usage(response["usage"]),
    )

    assert isinstance(result, CostData)
    assert result.input_tokens == 700
    assert result.output_tokens == 60
    assert result.cache_read_input_tokens == 4300


@pytest.mark.asyncio
async def test_explicit_usage_param_wins_over_response_extraction() -> None:
    """When a normalized usage is passed, calculate_cost must not re-derive
    token counts from the raw response."""
    response = {
        "model": "dumb-1",
        "usage": {"prompt_tokens": 999999, "completion_tokens": 999999},
    }

    result = await calculate_cost(
        response,
        max_cost=100000,
        session=AsyncMock(),
        usage=NormalizedUsage(input_tokens=10, output_tokens=5),
    )

    assert isinstance(result, CostData)
    assert result.input_tokens == 10
    assert result.output_tokens == 5
