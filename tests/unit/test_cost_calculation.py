"""Unit tests for cost calculation functionality."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from routstr.payment.cost_caculation import (
    CostData,
    CostDataError,
    MaxCostData,
    calculate_cost,
)


@pytest.mark.asyncio
async def test_calculate_cost_with_all_token_types() -> None:
    """Test cost calculation with all token types."""
    from routstr.core.settings import settings
    
    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", False):
        with patch("routstr.proxy.get_model_instance") as mock_get_model:
            mock_model = MagicMock()
            mock_pricing = MagicMock()
            mock_pricing.prompt = 0.03
            mock_pricing.completion = 0.06
            mock_model.sats_pricing = mock_pricing
            mock_get_model.return_value = mock_model
            
            result = await calculate_cost(response_data, 100000, mock_session)
            
            assert isinstance(result, CostData)
            assert result.input_msats > 0
            assert result.output_msats > 0
            assert result.total_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_missing_usage() -> None:
    """Test cost calculation with missing usage data."""
    response_data = {
        "model": "gpt-4",
    }
    
    mock_session = AsyncMock()
    
    result = await calculate_cost(response_data, 100000, mock_session)
    
    assert isinstance(result, MaxCostData)
    assert result.total_msats == 100000


@pytest.mark.asyncio
async def test_calculate_cost_invalid_model() -> None:
    """Test cost calculation with invalid model."""
    response_data = {
        "model": "invalid-model",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", False):
        with patch("routstr.proxy.get_model_instance", return_value=None):
            result = await calculate_cost(response_data, 100000, mock_session)
            
            assert isinstance(result, CostDataError)
            assert result.code == "model_not_found"


@pytest.mark.asyncio
async def test_calculate_cost_zero_tokens() -> None:
    """Test cost calculation with zero tokens."""
    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", True):
        result = await calculate_cost(response_data, 100000, mock_session)
        
        assert isinstance(result, MaxCostData)
        assert result.total_msats == 100000


@pytest.mark.asyncio
async def test_calculate_cost_very_large_tokens() -> None:
    """Test cost calculation with very large token counts."""
    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1000000,
            "completion_tokens": 500000,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", False):
        with patch("routstr.proxy.get_model_instance") as mock_get_model:
            mock_model = MagicMock()
            mock_pricing = MagicMock()
            mock_pricing.prompt = 0.03
            mock_pricing.completion = 0.06
            mock_model.sats_pricing = mock_pricing
            mock_get_model.return_value = mock_model
            
            result = await calculate_cost(response_data, 100000, mock_session)
            
            assert isinstance(result, CostData)
            assert result.total_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_with_provider_fee() -> None:
    """Test cost calculation with provider fee."""
    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", False):
        with patch("routstr.proxy.get_model_instance") as mock_get_model:
            mock_model = MagicMock()
            mock_pricing = MagicMock()
            mock_pricing.prompt = 0.03
            mock_pricing.completion = 0.06
            mock_model.sats_pricing = mock_pricing
            mock_get_model.return_value = mock_model
            
            result = await calculate_cost(response_data, 100000, mock_session)
            
            assert isinstance(result, CostData)
            assert result.total_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_with_images() -> None:
    """Test cost calculation with image tokens in usage."""
    response_data = {
        "model": "gpt-4-vision",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", False):
        with patch("routstr.proxy.get_model_instance") as mock_get_model:
            mock_model = MagicMock()
            mock_pricing = MagicMock()
            mock_pricing.prompt = 0.03
            mock_pricing.completion = 0.06
            mock_model.sats_pricing = mock_pricing
            mock_get_model.return_value = mock_model
            
            result = await calculate_cost(response_data, 100000, mock_session)
            
            assert isinstance(result, CostData)


@pytest.mark.asyncio
async def test_calculate_cost_with_web_search() -> None:
    """Test cost calculation with web search tokens."""
    response_data = {
        "model": "perplexity-model",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", False):
        with patch("routstr.proxy.get_model_instance") as mock_get_model:
            mock_model = MagicMock()
            mock_pricing = MagicMock()
            mock_pricing.prompt = 0.03
            mock_pricing.completion = 0.06
            mock_model.sats_pricing = mock_pricing
            mock_get_model.return_value = mock_model
            
            result = await calculate_cost(response_data, 100000, mock_session)
            
            assert isinstance(result, CostData)


@pytest.mark.asyncio
async def test_calculate_cost_with_reasoning_tokens() -> None:
    """Test cost calculation with internal reasoning tokens."""
    response_data = {
        "model": "o1",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", False):
        with patch("routstr.proxy.get_model_instance") as mock_get_model:
            mock_model = MagicMock()
            mock_pricing = MagicMock()
            mock_pricing.prompt = 0.03
            mock_pricing.completion = 0.06
            mock_model.sats_pricing = mock_pricing
            mock_get_model.return_value = mock_model
            
            result = await calculate_cost(response_data, 100000, mock_session)
            
            assert isinstance(result, CostData)


@pytest.mark.asyncio
async def test_calculate_cost_fixed_pricing() -> None:
    """Test cost calculation with fixed pricing mode."""
    response_data = {
        "model": "any-model",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", True):
        result = await calculate_cost(response_data, 100000, mock_session)
        
        assert isinstance(result, MaxCostData)
        assert result.total_msats == 100000


@pytest.mark.asyncio
async def test_calculate_cost_pricing_not_found() -> None:
    """Test cost calculation when model pricing not found."""
    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }
    
    mock_session = AsyncMock()
    
    with patch("routstr.payment.cost_caculation.settings.fixed_pricing", False):
        with patch("routstr.proxy.get_model_instance") as mock_get_model:
            mock_model = MagicMock()
            mock_model.sats_pricing = None
            mock_get_model.return_value = mock_model
            
            result = await calculate_cost(response_data, 100000, mock_session)
            
            assert isinstance(result, CostDataError)
            assert result.code == "pricing_not_found"
