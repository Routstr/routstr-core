"""Tests for cache token handling in cost calculation.

Covers OpenAI vs Anthropic caching formats, edge cases, and billing accuracy.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")
os.environ.setdefault("LIGHTNING_ADDRESS", "test@stm.to")

from routstr.core.settings import settings
from routstr.payment.cost_calculation import CostData, MaxCostData, calculate_cost


@pytest.fixture
def mock_session() -> AsyncMock:
    """Mock AsyncSession for cost calculation tests."""
    return AsyncMock()


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
async def test_openai_cache_subtraction(mock_session: AsyncMock) -> None:
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
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 1000  # 2000 - 1000
    assert result.cache_read_input_tokens == 1000
    assert result.output_tokens == 100


# ============================================================================
# Test 2: Anthropic Cache Format
# ============================================================================
@pytest.mark.asyncio
async def test_anthropic_cache_additive(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
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
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 500
    assert result.cache_creation_input_tokens == 1500
    assert result.cache_read_input_tokens == 0
    assert result.output_tokens == 100


# ============================================================================
# Test 3: Invalid Cache (Edge Case)
# ============================================================================
@pytest.mark.asyncio
async def test_cache_read_exceeds_prompt_tokens(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
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
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    # Should not go negative
    assert isinstance(result, CostData)
    assert result.input_tokens == 0  # max(0, 100 - 150)
    assert result.cache_read_input_tokens == 150
    assert result.output_tokens == 50


# ============================================================================
# Test 4: Malformed Token Values
# ============================================================================
@pytest.mark.asyncio
async def test_malformed_cache_tokens_coerce_to_zero(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
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
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    # Both should coerce to 0
    assert isinstance(result, CostData)
    assert result.cache_read_input_tokens == 0
    assert result.input_tokens == 100  # No subtraction if cache_read = 0


# ============================================================================
# Test 5: Anthropic Cache Not Subtracted
# ============================================================================
@pytest.mark.asyncio
async def test_anthropic_cache_not_subtracted(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
    """Anthropic cache fields should NOT be subtracted from input_tokens."""
    response = {
        "model": "claude-3-5-sonnet",
        "usage": {
            "input_tokens": 500,
            "completion_tokens": 100,
            "cache_read_input_tokens": 200,  # ← Additive, don't subtract
        }
    }
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    # Anthropic: input_tokens stays as-is
    assert isinstance(result, CostData)
    assert result.input_tokens == 500  # NOT 300
    assert result.cache_read_input_tokens == 200


# ============================================================================
# Test 6: Only Cache Read, No Regular Input
# ============================================================================
@pytest.mark.asyncio
async def test_only_cache_read_tokens(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
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
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 0  # max(0, 0 - 1000)
    assert result.cache_read_input_tokens == 1000
    assert result.output_tokens == 50


# ============================================================================
# Test 7: Only Cache Creation
# ============================================================================
@pytest.mark.asyncio
async def test_only_cache_creation_tokens(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
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
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 500
    assert result.cache_creation_input_tokens == 2000
    assert result.cache_read_input_tokens == 0
    assert result.output_tokens == 100


# ============================================================================
# Test 8: Both Cache Read and Creation
# ============================================================================
@pytest.mark.asyncio
async def test_both_cache_read_and_creation(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
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
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 300
    assert result.cache_creation_input_tokens == 2000
    assert result.cache_read_input_tokens == 500
    assert result.output_tokens == 100


# ============================================================================
# Test 9: Token Field Fallback
# ============================================================================
@pytest.mark.asyncio
async def test_token_field_fallback_order(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
    """Verify fallback order for token extraction."""
    # When prompt_tokens is not present, fall back to input_tokens
    response = {
        "model": "gpt-4",
        "usage": {
            "input_tokens": 250,
            "completion_tokens": 50,
        }
    }
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 250
    assert result.output_tokens == 50


# ============================================================================
# Test 10: Float Token Values
# ============================================================================
@pytest.mark.asyncio
async def test_float_token_values_coerced_to_int(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
    """Handle float token values by converting to int."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 100.7,  # Float
            "completion_tokens": 50.3,  # Float
            "cache_read_input_tokens": 25.9,  # Float
        }
    }
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 100  # Floored
    assert result.output_tokens == 50  # Floored
    assert result.cache_read_input_tokens == 25  # Floored


# ============================================================================
# Test 11: Boolean Cache Tokens
# ============================================================================
@pytest.mark.asyncio
async def test_boolean_cache_tokens_coerced_to_zero(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
    """Handle boolean cache token values by coercing to zero."""
    response = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cache_read_input_tokens": True,  # Boolean
        }
    }
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.cache_read_input_tokens == 0  # Boolean coerced to 0
    assert result.input_tokens == 100  # No subtraction


# ============================================================================
# Test 12: Zero Cache Tokens
# ============================================================================
@pytest.mark.asyncio
async def test_zero_cache_tokens(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
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
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.cache_read_input_tokens == 0
    assert result.input_tokens == 100


# ============================================================================
# Test 12b: DeepSeek Cache Hit/Miss Format
# ============================================================================
@pytest.mark.asyncio
async def test_deepseek_cache_hit_subtraction(
    mock_session: AsyncMock, mock_fixed_pricing: None
) -> None:
    """DeepSeek prompt_tokens = hit + miss; the hit is a cache read, subtract it."""
    response = {
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 10000,  # ← hit + miss
            "completion_tokens": 200,
            "prompt_cache_hit_tokens": 9000,  # ← cache read (0.1x upstream)
            "prompt_cache_miss_tokens": 1000,  # ← uncached input
        },
    }
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 1000  # 10000 - 9000
    assert result.cache_read_input_tokens == 9000
    assert result.cache_creation_input_tokens == 0  # DeepSeek has no cache write
    assert result.output_tokens == 200


@pytest.mark.asyncio
async def test_deepseek_no_cache_hit(
    mock_session: AsyncMock, mock_fixed_pricing: None
) -> None:
    """All-miss DeepSeek response leaves input_tokens untouched."""
    response = {
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 1000,
        },
    }
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 1000
    assert result.cache_read_input_tokens == 0


@pytest.mark.asyncio
async def test_anthropic_fields_take_priority_over_deepseek(
    mock_session: AsyncMock, mock_fixed_pricing: None
) -> None:
    """Explicit cache_read_input_tokens wins; DeepSeek hit must not double-subtract."""
    response = {
        "model": "claude-3-5-sonnet",
        "usage": {
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_read_input_tokens": 200,
            "prompt_cache_hit_tokens": 9999,  # ← ignored, Anthropic field present
        },
    }
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 500  # not subtracted
    assert result.cache_read_input_tokens == 200


# ============================================================================
# Test 12c: DeepSeek billed at cache rate, not full input (the actual bug)
# ============================================================================
@pytest.mark.asyncio
async def test_deepseek_cache_hits_billed_at_cache_rate(
    mock_session: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10k prompt at 90% cache hit must bill hits at the cache-read rate.

    Regression for the overcharge bug: cached prompt tokens were billed at the
    full input rate. With per-token prompt rate P and cache-read rate 0.1*P:
      honest  = 1000*P  (uncached) + 9000*0.1*P (cache read) = 1900*P
      buggy   = 10000*P (all at full input rate)
    """
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "fixed_pricing", False)

    # per-token sats rates; cache read is 0.1x the prompt rate (DeepSeek upstream)
    prompt_rate = 1.0e-6
    sats_pricing = SimpleNamespace(
        prompt=prompt_rate,
        completion=2.0e-6,
        input_cache_read=prompt_rate * 0.1,
        input_cache_write=0.0,
    )
    model_obj = SimpleNamespace(sats_pricing=sats_pricing)
    monkeypatch.setattr(
        "routstr.proxy.get_model_instance", lambda _id: model_obj
    )

    response = {
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 10000,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 9000,
            "prompt_cache_miss_tokens": 1000,
        },
    }
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, CostData)
    assert result.input_tokens == 1000
    assert result.cache_read_input_tokens == 9000

    # input_rate (msats/1k) = prompt_rate * 1e6 ; per-token msats = prompt_rate*1000
    per_tok_input_msats = prompt_rate * 1000
    expected_input = round(1000 / 1000 * (prompt_rate * 1e6), 3)
    expected_cache = round(9000 / 1000 * (prompt_rate * 0.1 * 1e6), 3)
    assert result.input_msats == int(expected_input)
    assert result.cache_read_msats == int(expected_cache)
    # honest total (1900*P) must be far below the buggy all-input charge (10000*P)
    buggy_total = 10000 * per_tok_input_msats
    assert result.total_msats < buggy_total * 0.25


# ============================================================================
# Test 13: Missing Usage Block
# ============================================================================
@pytest.mark.asyncio
async def test_missing_usage_block(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
    """When usage is missing, return MaxCostData with zero tokens."""
    response = {"model": "gpt-4", "choices": [{"message": {"content": "test"}}]}
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, MaxCostData)
    assert result.input_tokens == 0
    assert result.cache_read_input_tokens == 0
    assert result.output_tokens == 0


# ============================================================================
# Test 14: Null Usage Block
# ============================================================================
@pytest.mark.asyncio
async def test_null_usage_block(mock_session: AsyncMock, mock_fixed_pricing: None) -> None:
    """When usage is null, return MaxCostData with zero tokens."""
    response = {"model": "gpt-4", "usage": None}
    result = await calculate_cost(response, max_cost=100000, session=mock_session)

    assert isinstance(result, MaxCostData)
    assert result.input_tokens == 0
    assert result.cache_read_input_tokens == 0
