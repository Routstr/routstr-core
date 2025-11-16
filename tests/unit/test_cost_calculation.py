import os
from unittest.mock import AsyncMock, Mock, patch

os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

import pytest
from routstr.core.settings import settings
from routstr.payment.cost_caculation import (
    CostData,
    CostDataError,
    MaxCostData,
    calculate_cost,
)


@pytest.mark.asyncio
async def test_calculate_cost_with_usage_data() -> None:
    from routstr.payment.models import Pricing

    mock_session = AsyncMock()
    mock_model = Mock()
    mock_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
    )
    mock_model.sats_pricing = mock_pricing

    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }

    with patch("routstr.payment.cost_caculation.get_model_instance", return_value=mock_model):
        with patch.object(settings, "fixed_pricing", False):
            result = await calculate_cost(response_data, 1000000, mock_session)

            assert isinstance(result, CostData)
            assert result.input_msats > 0
            assert result.output_msats > 0
            assert result.total_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_without_usage_data() -> None:
    mock_session = AsyncMock()

    response_data = {
        "model": "gpt-4",
    }

    result = await calculate_cost(response_data, 1000000, mock_session)

    assert isinstance(result, MaxCostData)
    assert result.total_msats == 1000000
    assert result.base_msats == 1000000


@pytest.mark.asyncio
async def test_calculate_cost_with_fixed_pricing() -> None:
    mock_session = AsyncMock()

    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }

    with patch.object(settings, "fixed_pricing", True):
        with patch.object(settings, "fixed_per_1k_input_tokens", 0.001):
            with patch.object(settings, "fixed_per_1k_output_tokens", 0.002):
                result = await calculate_cost(response_data, 1000000, mock_session)

                assert isinstance(result, CostData)
                assert result.input_msats > 0
                assert result.output_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_invalid_model() -> None:
    mock_session = AsyncMock()

    response_data = {
        "model": "invalid-model",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }

    with patch("routstr.payment.cost_caculation.get_model_instance", return_value=None):
        with patch.object(settings, "fixed_pricing", False):
            result = await calculate_cost(response_data, 1000000, mock_session)

            assert isinstance(result, CostDataError)
            assert result.code == "model_not_found"


@pytest.mark.asyncio
async def test_calculate_cost_no_pricing() -> None:
    from routstr.payment.models import Pricing

    mock_session = AsyncMock()
    mock_model = Mock()
    mock_model.sats_pricing = None

    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }

    with patch("routstr.payment.cost_caculation.get_model_instance", return_value=mock_model):
        with patch.object(settings, "fixed_pricing", False):
            result = await calculate_cost(response_data, 1000000, mock_session)

            assert isinstance(result, CostDataError)
            assert result.code == "pricing_not_found"


@pytest.mark.asyncio
async def test_calculate_cost_zero_tokens() -> None:
    from routstr.payment.models import Pricing

    mock_session = AsyncMock()
    mock_model = Mock()
    mock_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
    )
    mock_model.sats_pricing = mock_pricing

    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
        },
    }

    with patch("routstr.payment.cost_caculation.get_model_instance", return_value=mock_model):
        with patch.object(settings, "fixed_pricing", False):
            result = await calculate_cost(response_data, 1000000, mock_session)

            assert isinstance(result, CostData)
            assert result.input_msats == 0
            assert result.output_msats == 0
            assert result.total_msats == 0


@pytest.mark.asyncio
async def test_calculate_cost_very_large_tokens() -> None:
    from routstr.payment.models import Pricing

    mock_session = AsyncMock()
    mock_model = Mock()
    mock_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
    )
    mock_model.sats_pricing = mock_pricing

    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1000000,
            "completion_tokens": 500000,
        },
    }

    with patch("routstr.payment.cost_caculation.get_model_instance", return_value=mock_model):
        with patch.object(settings, "fixed_pricing", False):
            result = await calculate_cost(response_data, 1000000000, mock_session)

            assert isinstance(result, CostData)
            assert result.input_msats > 0
            assert result.output_msats > 0
            assert result.total_msats > 0


@pytest.mark.asyncio
async def test_calculate_cost_missing_usage_fields() -> None:
    from routstr.payment.models import Pricing

    mock_session = AsyncMock()
    mock_model = Mock()
    mock_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
    )
    mock_model.sats_pricing = mock_pricing

    response_data = {
        "model": "gpt-4",
        "usage": {},
    }

    with patch("routstr.payment.cost_caculation.get_model_instance", return_value=mock_model):
        with patch.object(settings, "fixed_pricing", False):
            result = await calculate_cost(response_data, 1000000, mock_session)

            assert isinstance(result, CostData)
            assert result.input_msats == 0
            assert result.output_msats == 0


@pytest.mark.asyncio
async def test_calculate_cost_with_provider_fee() -> None:
    from routstr.payment.models import Pricing

    mock_session = AsyncMock()
    mock_model = Mock()
    mock_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
    )
    mock_model.sats_pricing = mock_pricing

    response_data = {
        "model": "gpt-4",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 500,
        },
    }

    with patch("routstr.payment.cost_caculation.get_model_instance", return_value=mock_model):
        with patch.object(settings, "fixed_pricing", False):
            result = await calculate_cost(response_data, 1000000, mock_session)

            assert isinstance(result, CostData)
            assert result.total_msats > 0
