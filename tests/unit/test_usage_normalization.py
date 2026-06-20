"""Tests for vendor-agnostic usage normalization.

Specifies the seam that keeps vendor usage dialects out of generic billing
code: a canonical ``NormalizedUsage`` shape produced by
``routstr.payment.usage.normalize_usage`` (union parser for the known,
non-colliding dialects). ``calculate_cost`` normalizes the response's usage
object with this parser and needs no vendor knowledge of its own.
"""

import os

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

import pytest

from routstr.payment.usage import NormalizedUsage, normalize_usage

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
        # OpenRouter: cache writes nested as prompt_tokens_details.cache_write_tokens,
        # both reads and writes included in prompt_tokens → both subtracted
        (
            {
                "prompt_tokens": 10000,
                "completion_tokens": 60,
                "prompt_tokens_details": {
                    "cached_tokens": 5000,
                    "cache_write_tokens": 2000,
                },
            },
            NormalizedUsage(
                input_tokens=3000,
                output_tokens=60,
                cache_read_tokens=5000,
                cache_write_tokens=2000,
            ),
        ),
        # litellm-normalized Anthropic: prompt_tokens is the grand total and the
        # write field is named cache_creation_tokens; top-level fields mirror it.
        # prompt_tokens present → both subtracted (NOT additive like native).
        (
            {
                "prompt_tokens": 10000,
                "completion_tokens": 100,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 2000,
                "prompt_tokens_details": {
                    "cached_tokens": 5000,
                    "cache_creation_tokens": 2000,
                },
            },
            NormalizedUsage(
                input_tokens=3000,
                output_tokens=100,
                cache_read_tokens=5000,
                cache_write_tokens=2000,
            ),
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
