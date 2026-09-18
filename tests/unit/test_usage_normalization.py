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

from routstr.payment.usage import (
    NormalizedUsage,
    UsageFieldPresence,
    ledger_usage,
    normalize_usage,
    usage_field_presence,
)

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


def test_responses_cache_split_is_ledger_only() -> None:
    usage = {
        "input_tokens": 100,
        "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 20},
    }

    assert normalize_usage(usage) == NormalizedUsage(input_tokens=100, output_tokens=5)
    assert ledger_usage(usage) == NormalizedUsage(
        input_tokens=80, output_tokens=5, cache_read_tokens=20
    )


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


def test_usage_presence_counts_explicit_zero_across_dialects() -> None:
    presence = usage_field_presence(
        {
            "prompt_tokens": 0,
            "completion_tokens": "0",
            "prompt_tokens_details": {
                "cached_tokens": 0.0,
                "cache_write_tokens": "0",
            },
        }
    )

    assert presence == UsageFieldPresence(
        input_observed=True,
        output_observed=True,
        cache_read_observed=True,
        cache_creation_observed=True,
    )


def test_usage_presence_rejects_missing_and_unparseable_fields() -> None:
    presence = usage_field_presence(
        {
            "input_tokens": None,
            "output_tokens": "not-a-number",
            "cache_read_input_tokens": -1,
            "cache_creation_input_tokens": False,
        }
    )

    assert presence == UsageFieldPresence()


def test_usage_presence_recognizes_responses_cache_details() -> None:
    presence = usage_field_presence(
        {
            "input_tokens": 12,
            "output_tokens": 3,
            "input_tokens_details": {"cached_tokens": 0},
        }
    )

    assert presence.input_observed is True
    assert presence.output_observed is True
    assert presence.cache_read_observed is True
    assert presence.cache_creation_observed is False


def test_locally_estimated_usage_is_not_observed() -> None:
    presence = usage_field_presence(
        {
            "input_tokens": 12,
            "output_tokens": 3,
            "estimated": True,
        }
    )

    assert presence.input_observed is False
    assert presence.output_observed is False
    assert presence.sources_dict() == {
        "input_source": "estimated",
        "output_source": "estimated",
        "cache_read_source": "missing",
        "cache_creation_source": "missing",
    }
