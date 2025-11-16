"""Unit tests for cost calculation functionality."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.payment.cost_caculation import CostData, CostDataError, MaxCostData, calculate_cost
from routstr.payment.models import Model, SatsPricing


@pytest.fixture
def mock_session() -> AsyncSession:
    """Fixture to provide a mock database session."""
    return MagicMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_calculate_cost_with_all_token_types(mock_session: AsyncSession) -> None:
    """Test cost calculation with input and output tokens."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = True
        mock_settings.fixed_per_1k_input_tokens = 0.01
        mock_settings.fixed_per_1k_output_tokens = 0.02
        
        response_data = {
            "model": "gpt-4",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            },
        }
        max_cost = 50000
        
        result = await calculate_cost(response_data, max_cost, mock_session)
        
        assert isinstance(result, CostData)
        assert result.total_msats > 0
        assert result.input_msats > 0
        assert result.output_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_missing_usage(mock_session: AsyncSession) -> None:
    """Test cost calculation when usage data is missing."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = True
        mock_settings.fixed_per_1k_input_tokens = 0.01
        mock_settings.fixed_per_1k_output_tokens = 0.02
        
        response_data = {"model": "gpt-4"}
        max_cost = 50000
        
        result = await calculate_cost(response_data, max_cost, mock_session)
        
        assert isinstance(result, MaxCostData)
        assert result.total_msats == max_cost
        assert result.base_msats == max_cost


@pytest.mark.asyncio
async def test_calculate_cost_invalid_model(mock_session: AsyncSession) -> None:
    """Test cost calculation with invalid model."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = False
        
        with patch("routstr.payment.cost_caculation.get_model_instance") as mock_get_model:
            mock_get_model.return_value = None
            
            response_data = {
                "model": "invalid-model",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                },
            }
            max_cost = 50000
            
            result = await calculate_cost(response_data, max_cost, mock_session)
            
            assert isinstance(result, CostDataError)
            assert result.code == "model_not_found"


@pytest.mark.asyncio
async def test_calculate_cost_zero_tokens(mock_session: AsyncSession) -> None:
    """Test cost calculation with zero tokens."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = True
        mock_settings.fixed_per_1k_input_tokens = 0.01
        mock_settings.fixed_per_1k_output_tokens = 0.02
        
        response_data = {
            "model": "gpt-4",
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            },
        }
        max_cost = 50000
        
        result = await calculate_cost(response_data, max_cost, mock_session)
        
        assert isinstance(result, CostData)
        assert result.total_msats == 0


@pytest.mark.asyncio
async def test_calculate_cost_very_large_tokens(mock_session: AsyncSession) -> None:
    """Test cost calculation with very large token counts."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = True
        mock_settings.fixed_per_1k_input_tokens = 0.01
        mock_settings.fixed_per_1k_output_tokens = 0.02
        
        response_data = {
            "model": "gpt-4",
            "usage": {
                "prompt_tokens": 100000,
                "completion_tokens": 50000,
            },
        }
        max_cost = 5000000
        
        result = await calculate_cost(response_data, max_cost, mock_session)
        
        assert isinstance(result, CostData)
        assert result.total_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_with_model_based_pricing(mock_session: AsyncSession) -> None:
    """Test cost calculation using model-specific pricing."""
    mock_model = Model(
        id="gpt-4",
        name="gpt-4",
        created=0,
        description="Test model",
        context_length=8192,
        architecture={"modality": "text"},
        pricing={"input": 0.03, "output": 0.06},
        sats_pricing=SatsPricing(prompt=0.03, completion=0.06),
    )
    
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = False
        
        with patch("routstr.payment.cost_caculation.get_model_instance") as mock_get_model:
            mock_get_model.return_value = mock_model
            
            response_data = {
                "model": "gpt-4",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                },
            }
            max_cost = 100000
            
            result = await calculate_cost(response_data, max_cost, mock_session)
            
            assert isinstance(result, CostData)
            assert result.total_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_model_without_pricing(mock_session: AsyncSession) -> None:
    """Test cost calculation when model has no pricing data."""
    mock_model = Model(
        id="free-model",
        name="free-model",
        created=0,
        description="Test model",
        context_length=8192,
        architecture={"modality": "text"},
        pricing={"input": 0, "output": 0},
        sats_pricing=None,
    )
    
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = False
        
        with patch("routstr.payment.cost_caculation.get_model_instance") as mock_get_model:
            mock_get_model.return_value = mock_model
            
            response_data = {
                "model": "free-model",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                },
            }
            max_cost = 50000
            
            result = await calculate_cost(response_data, max_cost, mock_session)
            
            assert isinstance(result, CostDataError)
            assert result.code == "pricing_not_found"


@pytest.mark.asyncio
async def test_calculate_cost_with_zero_pricing_config(mock_session: AsyncSession) -> None:
    """Test cost calculation when pricing is configured to zero."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = True
        mock_settings.fixed_per_1k_input_tokens = 0.0
        mock_settings.fixed_per_1k_output_tokens = 0.0
        
        response_data = {
            "model": "gpt-4",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
            },
        }
        max_cost = 50000
        
        result = await calculate_cost(response_data, max_cost, mock_session)
        
        assert isinstance(result, MaxCostData)
        assert result.total_msats == max_cost


@pytest.mark.asyncio
async def test_calculate_cost_usage_is_none(mock_session: AsyncSession) -> None:
    """Test cost calculation when usage is explicitly None."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = True
        mock_settings.fixed_per_1k_input_tokens = 0.01
        mock_settings.fixed_per_1k_output_tokens = 0.02
        
        response_data = {"model": "gpt-4", "usage": None}
        max_cost = 50000
        
        result = await calculate_cost(response_data, max_cost, mock_session)
        
        assert isinstance(result, MaxCostData)
        assert result.total_msats == max_cost


@pytest.mark.asyncio
async def test_calculate_cost_rounds_up_fractional_msats(mock_session: AsyncSession) -> None:
    """Test that fractional millisats are rounded up."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = True
        mock_settings.fixed_per_1k_input_tokens = 0.001
        mock_settings.fixed_per_1k_output_tokens = 0.001
        
        response_data = {
            "model": "gpt-4",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 100,
            },
        }
        max_cost = 50000
        
        result = await calculate_cost(response_data, max_cost, mock_session)
        
        assert isinstance(result, CostData)
        assert result.total_msats >= 1


@pytest.mark.asyncio
async def test_calculate_cost_with_only_input_tokens(mock_session: AsyncSession) -> None:
    """Test cost calculation with only input tokens."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = True
        mock_settings.fixed_per_1k_input_tokens = 0.01
        mock_settings.fixed_per_1k_output_tokens = 0.02
        
        response_data = {
            "model": "gpt-4",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 0,
            },
        }
        max_cost = 50000
        
        result = await calculate_cost(response_data, max_cost, mock_session)
        
        assert isinstance(result, CostData)
        assert result.input_msats > 0
        assert result.output_msats == 0


@pytest.mark.asyncio
async def test_calculate_cost_with_only_output_tokens(mock_session: AsyncSession) -> None:
    """Test cost calculation with only output tokens."""
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = True
        mock_settings.fixed_per_1k_input_tokens = 0.01
        mock_settings.fixed_per_1k_output_tokens = 0.02
        
        response_data = {
            "model": "gpt-4",
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 1000,
            },
        }
        max_cost = 50000
        
        result = await calculate_cost(response_data, max_cost, mock_session)
        
        assert isinstance(result, CostData)
        assert result.input_msats == 0
        assert result.output_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_with_invalid_pricing_data(mock_session: AsyncSession) -> None:
    """Test cost calculation when model has invalid pricing format."""
    mock_model = Model(
        id="invalid-pricing-model",
        name="invalid-pricing-model",
        created=0,
        description="Test model",
        context_length=8192,
        architecture={"modality": "text"},
        pricing={"input": "invalid", "output": "invalid"},
        sats_pricing=SatsPricing(prompt="invalid", completion="invalid"),  # type: ignore
    )
    
    with patch("routstr.payment.cost_caculation.settings") as mock_settings:
        mock_settings.fixed_pricing = False
        
        with patch("routstr.payment.cost_caculation.get_model_instance") as mock_get_model:
            mock_get_model.return_value = mock_model
            
            response_data = {
                "model": "invalid-pricing-model",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                },
            }
            max_cost = 50000
            
            result = await calculate_cost(response_data, max_cost, mock_session)
            
            assert isinstance(result, CostDataError)
            assert result.code == "pricing_invalid"
