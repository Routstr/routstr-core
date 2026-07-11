"""Tests for cache token handling in cost calculation.

Covers OpenAI, Anthropic and DeepSeek caching formats, dialect precedence,
edge cases, and billing accuracy.
"""

import os
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from routstr.core.settings import settings
from routstr.payment.cost_calculation import CostData, MaxCostData, calculate_cost


@pytest.fixture(autouse=True)
def mock_fixed_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock settings and price to use fixed pricing."""
    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", 0.001)
    monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", 0.001)


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> None:  # type: ignore[misc]
    """Patch sats_usd_price to avoid initialization issues."""
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-5):
        yield


# ============================================================================
# Test 1: OpenAI Cache Format
# ============================================================================
@pytest.mark.asyncio
async def test_openai_cache_subtraction() -> None:
    """OpenAI includes cached_tokens in prompt_tokens, subtract them."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 2000,  # ← Includes 1000 cached
            "completion_tokens": 100,
            "prompt_tokens_details": {
                "cached_tokens": 1000  # ← Extracted separately
            }
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 1000  # 2000 - 1000
    assert result.cache_read_input_tokens == 1000
    assert result.output_tokens == 100


# ============================================================================
# Test 2: Anthropic Cache Format
# ============================================================================
@pytest.mark.asyncio
async def test_anthropic_cache_additive(mock_fixed_pricing: None) -> None:
    """Anthropic cache tokens are separate (additive) from input_tokens."""
    response = {
        "model": "claude-3-5-sonnet",
        "usage": {
            "input_tokens": 500,  # ← Regular input only
            "output_tokens": 100,
            "cache_creation_input_tokens": 1500,  # ← Additive, not included above
            "cache_read_input_tokens": 0,
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 500
    assert result.cache_creation_input_tokens == 1500
    assert result.cache_read_input_tokens == 0
    assert result.output_tokens == 100


# ============================================================================
# Test 3: Invalid Cache (Edge Case)
# ============================================================================
@pytest.mark.asyncio
async def test_cache_read_exceeds_prompt_tokens(mock_fixed_pricing: None) -> None:
    """Handle buggy upstream reporting cached > prompt_tokens."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {
                "cached_tokens": 150  # ← Invalid! Greater than prompt
            }
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    # Should not go negative
    assert isinstance(result, CostData)
    assert result.input_tokens == 0  # max(0, 100 - 150)
    assert result.cache_read_input_tokens == 150
    assert result.output_tokens == 50


# ============================================================================
# Test 4: Malformed Token Values
# ============================================================================
@pytest.mark.asyncio
async def test_malformed_cache_tokens_coerce_to_zero(mock_fixed_pricing: None) -> None:
    """Handle non-numeric cache token values."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cache_read_input_tokens": "-50",  # ← String, negative
            "prompt_tokens_details": {
                "cached_tokens": "invalid"  # ← Non-numeric string
            }
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    # Both should coerce to 0
    assert isinstance(result, CostData)
    assert result.cache_read_input_tokens == 0
    assert result.input_tokens == 100  # No subtraction if cache_read = 0


# ============================================================================
# Test 5: Anthropic Cache Not Subtracted
# ============================================================================
@pytest.mark.asyncio
async def test_anthropic_cache_not_subtracted(mock_fixed_pricing: None) -> None:
    """Anthropic cache fields should NOT be subtracted from input_tokens."""
    response = {
        "model": "claude-3-5-sonnet",
        "usage": {
            "input_tokens": 500,
            "completion_tokens": 100,
            "cache_read_input_tokens": 200,  # ← Additive, don't subtract
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    # Anthropic: input_tokens stays as-is
    assert isinstance(result, CostData)
    assert result.input_tokens == 500  # NOT 300
    assert result.cache_read_input_tokens == 200


# ============================================================================
# Test 6: Only Cache Read, No Regular Input
# ============================================================================
@pytest.mark.asyncio
async def test_only_cache_read_tokens(mock_fixed_pricing: None) -> None:
    """Handle response with only cache read tokens."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 50,
            "prompt_tokens_details": {
                "cached_tokens": 1000
            }
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 0  # max(0, 0 - 1000)
    assert result.cache_read_input_tokens == 1000
    assert result.output_tokens == 50


# ============================================================================
# Test 7: Only Cache Creation
# ============================================================================
@pytest.mark.asyncio
async def test_only_cache_creation_tokens(mock_fixed_pricing: None) -> None:
    """Handle response with only cache creation tokens (Anthropic)."""
    response = {
        "model": "claude-3-5-sonnet",
        "usage": {
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 0,
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 500
    assert result.cache_creation_input_tokens == 2000
    assert result.cache_read_input_tokens == 0
    assert result.output_tokens == 100


# ============================================================================
# Test 8: Both Cache Read and Creation
# ============================================================================
@pytest.mark.asyncio
async def test_both_cache_read_and_creation(mock_fixed_pricing: None) -> None:
    """Handle response with both cache read and creation."""
    response = {
        "model": "claude-3-5-sonnet",
        "usage": {
            "input_tokens": 300,
            "output_tokens": 100,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 500,
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 300
    assert result.cache_creation_input_tokens == 2000
    assert result.cache_read_input_tokens == 500
    assert result.output_tokens == 100


# ============================================================================
# Test 9: Token Field Fallback
# ============================================================================
@pytest.mark.asyncio
async def test_token_field_fallback_order(mock_fixed_pricing: None) -> None:
    """Verify fallback order for token extraction."""
    # When prompt_tokens is not present, fall back to input_tokens
    response = {
        "model": "gpt-4",
        "usage": {
            "input_tokens": 250,
            "completion_tokens": 50,
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 250
    assert result.output_tokens == 50


# ============================================================================
# Test 10: Float Token Values
# ============================================================================
@pytest.mark.asyncio
async def test_float_token_values_coerced_to_int(mock_fixed_pricing: None) -> None:
    """Handle float token values by converting to int."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 100.7,  # Float
            "completion_tokens": 50.3,  # Float
            "prompt_tokens_details": {"cached_tokens": 25.9},  # Float
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    # cached_tokens are part of prompt_tokens (OpenAI dialect) → subtracted: 100 - 25
    assert result.input_tokens == 75  # Floored
    assert result.output_tokens == 50  # Floored
    assert result.cache_read_input_tokens == 25  # Floored


# ============================================================================
# Test 11: Boolean Cache Tokens
# ============================================================================
@pytest.mark.asyncio
async def test_boolean_cache_tokens_coerced_to_zero(mock_fixed_pricing: None) -> None:
    """Handle boolean cache token values by coercing to zero."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cache_read_input_tokens": True,  # Boolean
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.cache_read_input_tokens == 0  # Boolean coerced to 0
    assert result.input_tokens == 100  # No subtraction


# ============================================================================
# Test 12: Zero Cache Tokens
# ============================================================================
@pytest.mark.asyncio
async def test_zero_cache_tokens(mock_fixed_pricing: None) -> None:
    """Handle explicit zero cache tokens."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {
                "cached_tokens": 0
            }
        }
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.cache_read_input_tokens == 0
    assert result.input_tokens == 100


# ============================================================================
# DeepSeek Cache Format
# DeepSeek emits neither OpenAI's prompt_tokens_details nor Anthropic's
# cache_read_input_tokens — only prompt_cache_hit_tokens and
# prompt_cache_miss_tokens, with the documented guarantee
# prompt_tokens = hit + miss. Hits are ~10x cheaper upstream, so billing
# them as regular input is a large overcharge.
# ============================================================================
@pytest.mark.asyncio
async def test_deepseek_cache_hit_tokens_extracted() -> None:
    """DeepSeek cache hits are extracted and removed from regular input.

    Payload shape verbatim from the DeepSeek API reference (usage object).
    """
    response = {
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 10000,  # = hit + miss
            "completion_tokens": 500,
            "total_tokens": 10500,
            "prompt_cache_hit_tokens": 9000,
            "prompt_cache_miss_tokens": 1000,
        },
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 1000  # only the cache misses
    assert result.cache_read_input_tokens == 9000
    assert result.output_tokens == 500


@pytest.mark.asyncio
async def test_deepseek_all_tokens_cached() -> None:
    """A fully cached DeepSeek prompt bills zero regular input tokens."""
    response = {
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 5000,
            "completion_tokens": 100,
            "prompt_cache_hit_tokens": 5000,
            "prompt_cache_miss_tokens": 0,
        },
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 0
    assert result.cache_read_input_tokens == 5000


@pytest.mark.asyncio
async def test_dialect_precedence_never_double_subtracts() -> None:
    """If a vendor emits both OpenAI-style and DeepSeek-style cache fields for
    the same cached tokens, they are counted once, not subtracted twice."""
    response = {
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 10000,
            "completion_tokens": 500,
            "prompt_tokens_details": {"cached_tokens": 9000},
            "prompt_cache_hit_tokens": 9000,
            "prompt_cache_miss_tokens": 1000,
        },
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 1000  # 10000 - 9000, applied exactly once
    assert result.cache_read_input_tokens == 9000


@pytest.mark.asyncio
async def test_deepseek_malformed_hit_tokens_coerce_to_zero() -> None:
    """Malformed DeepSeek cache fields degrade to billing all input at full
    rate instead of crashing or going negative."""
    response = {
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_cache_hit_tokens": "garbage",
            "prompt_cache_miss_tokens": -5,
        },
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 1000
    assert result.cache_read_input_tokens == 0


# ============================================================================
# Truly-empty response with a non-zero USD cost → full refund
#
# When an upstream reports a USD cost but the response carries NO tokens at all
# (input, output, cache-read and cache-creation all zero), billing the
# USD-derived cost charges the user for nothing. Refund in full. The gate is
# tightened relative to PR #489: a cache-read/-creation-only turn legitimately
# reports zero prompt/completion tokens with a real cost and must still bill.
# ============================================================================
@pytest.mark.asyncio
async def test_truly_empty_usd_cost_response_is_refunded(
    mock_fixed_pricing: None,
) -> None:
    """0 input + 0 output + 0 cache tokens with a non-zero USD cost → refund."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_cost": 0.01,  # non-zero USD cost despite no tokens
        },
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.total_msats == 0  # full refund
    assert result.input_msats == 0
    assert result.output_msats == 0
    assert result.total_usd == 0.0
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cache_read_input_tokens == 0
    assert result.cache_creation_input_tokens == 0


@pytest.mark.asyncio
async def test_cache_read_only_usd_cost_response_is_billed(
    mock_fixed_pricing: None,
) -> None:
    """Cache-read-only turn (0 prompt/completion, non-zero cost) still bills."""
    response = {
        "model": "claude-3-5-sonnet",
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 1000,  # real cached usage
            "cache_creation_input_tokens": 0,
            "total_cost": 0.01,  # non-zero USD cost
        },
    }
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    # NOT refunded — the USD cost is billed in full. Pinning the exact value
    # guards against any future regression that would over-refund a cache-only
    # turn (the bug in PR #489, which refunded whenever prompt+completion == 0).
    assert result.total_msats == 200000
    assert result.total_usd == 0.01
    assert result.cache_read_input_tokens == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total_cost", "input_cost", "output_cost", "expected_msats"),
    [
        (0.000471, 0.00023451, 0.00023649, 9420),
        (0.00000004, 0.00000002, 0.00000002, 1),
    ],
)
async def test_small_usd_cost_components_sum_to_rounded_total(
    total_cost: float,
    input_cost: float,
    output_cost: float,
    expected_msats: int,
) -> None:
    """Small USD component costs must retain every billed millisatoshi."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "cost_details": {
                "total_cost": total_cost,
                "input_cost": input_cost,
                "output_cost": output_cost,
            },
        },
    }

    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.total_msats == expected_msats
    assert result.input_msats + result.output_msats == result.total_msats


@pytest.mark.asyncio
async def test_total_only_usd_cost_uses_model_prices_for_component_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reported total is split by priced tokens, not raw token counts."""
    monkeypatch.setattr(settings, "fixed_pricing", False)
    response = {
        "model": "z-ai/glm-5.2-20260616",
        "usage": {
            "prompt_tokens": 375,
            "completion_tokens": 218,
            "total_cost": 0.00039088,
        },
    }
    pricing = Mock(
        prompt=0.0001,
        completion=0.001,
        input_cache_read=0.0001,
        input_cache_write=0.0001,
    )
    model = Mock(sats_pricing=pricing)

    with patch("routstr.proxy.get_model_instance", return_value=model):
        result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.total_msats == 7818
    assert result.input_msats + result.output_msats == result.total_msats
    assert result.input_msats == 1147
    assert result.output_msats == 6671


@pytest.mark.asyncio
async def test_upstream_inference_cost_details_set_nonzero_components() -> None:
    """OpenAI-compatible upstream inference aliases retain their exact split."""
    response = {
        "model": "z-ai/glm-5.2-20260616",
        "usage": {
            "prompt_tokens": 211,
            "completion_tokens": 500,
            "total_tokens": 711,
            "cost": 0.00242155,
            "cost_details": {
                "upstream_inference_cost": 0.00242155,
                "upstream_inference_prompt_cost": 0.00022155,
                "upstream_inference_completions_cost": 0.0022,
            },
        },
    }

    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, CostData)
    assert result.input_tokens == 211
    assert result.output_tokens == 500
    assert result.input_msats == 4431
    assert result.output_msats == 44000
    assert result.total_msats == 48431
    assert result.input_msats + result.output_msats == result.total_msats


# ============================================================================
# Test 13: Missing Usage Block
# ============================================================================
@pytest.mark.asyncio
async def test_missing_usage_block(mock_fixed_pricing: None) -> None:
    """When usage is missing, return MaxCostData with zero tokens."""
    response = {"model": "gpt-4", "choices": [{"message": {"content": "test"}}]}
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, MaxCostData)
    assert result.input_tokens == 0
    assert result.cache_read_input_tokens == 0
    assert result.output_tokens == 0


# ============================================================================
# Test 14: Null Usage Block
# ============================================================================
@pytest.mark.asyncio
async def test_null_usage_block(mock_fixed_pricing: None) -> None:
    """When usage is null, return MaxCostData with zero tokens."""
    response = {"model": "gpt-4", "usage": None}
    result = await calculate_cost(response, max_cost=100000)

    assert isinstance(result, MaxCostData)
    assert result.input_tokens == 0
    assert result.cache_read_input_tokens == 0
