import os
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.core.settings import settings  # noqa: E402
from routstr.payment.helpers import get_max_cost_for_model  # noqa: E402


async def test_get_max_cost_for_model_known() -> None:
    from routstr.payment.models import Pricing

    # Mock DB session behavior
    mock_session = AsyncMock()

    # Mock upstream provider rows
    mock_provider_result = Mock()
    mock_provider_result.all = Mock(return_value=[])

    # Mock model row with proper JSON fields
    row = Mock()
    row.id = "gpt-4"
    row.name = "GPT-4"
    row.created = 1234567890
    row.description = "Test model"
    row.context_length = 8192
    row.architecture = '{"modality": "text", "input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "gpt", "instruct_type": null}'
    row.pricing = '{"prompt": 0.0, "completion": 0.0, "request": 0.0, "image": 0.0, "web_search": 0.0, "internal_reasoning": 0.0, "max_cost": 0.0}'
    row.per_request_limits = None
    row.top_provider = None
    row.enabled = True
    row.upstream_provider_id = 1

    # Mock the exec results to return model row when querying for override
    def mock_exec(query: Any) -> Any:
        result = Mock()
        result.first = Mock(return_value=row)
        result.all = Mock(return_value=[row])
        return result

    mock_session.exec = Mock(side_effect=mock_exec)

    # Mock get for UpstreamProviderRow
    mock_provider = Mock()
    mock_provider.provider_fee = 1.01
    mock_session.get = Mock(return_value=mock_provider)

    # Mock the model with sats_pricing
    mock_pricing = Pricing(
        prompt=0.0,
        completion=0.0,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
    )
    mock_model = Mock()
    mock_model.sats_pricing = mock_pricing

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 0):
            cost = await get_max_cost_for_model(
                "gpt-4", session=mock_session, model_obj=mock_model
            )
            assert cost == 500000  # 500 sats * 1000 = msats


async def test_get_max_cost_for_model_unknown() -> None:
    mock_session = AsyncMock()

    # Mock the exec results to return no model override
    async def async_mock_exec(query: Any) -> Any:
        result = Mock()
        result.first = Mock(return_value=None)
        result.all = Mock(return_value=[])
        return result

    mock_session.exec = AsyncMock(side_effect=async_mock_exec)
    mock_session.get = AsyncMock(return_value=None)

    # Mock get_upstreams to return empty list
    with patch("routstr.proxy.get_upstreams", return_value=[]):
        with patch.object(settings, "fixed_cost_per_request", 100):
            with patch.object(settings, "tolerance_percentage", 0):
                cost = await get_max_cost_for_model(
                    "unknown-model", session=mock_session, model_obj=None
                )
                assert cost == 100000


async def test_get_max_cost_for_model_disabled() -> None:
    mock_session = AsyncMock()
    with patch.object(settings, "fixed_pricing", True):
        with patch.object(settings, "fixed_cost_per_request", 200):
            with patch.object(settings, "tolerance_percentage", 0):
                cost = await get_max_cost_for_model("any-model", session=mock_session)
                assert cost == 200000


async def test_get_max_cost_for_model_tolerance() -> None:
    from routstr.payment.models import Pricing

    mock_session = AsyncMock()

    # Mock the model with sats_pricing
    mock_pricing = Pricing(
        prompt=0.0,
        completion=0.0,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
    )
    mock_model = Mock()
    mock_model.sats_pricing = mock_pricing

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 10):
            cost = await get_max_cost_for_model(
                "gpt-4", session=mock_session, model_obj=mock_model
            )
            assert cost == 450000  # 500 sats * 1000 * 0.9 = 450000


async def test_calculate_discounted_max_cost_basic() -> None:
    """Test basic discounted max cost calculation."""
    from routstr.payment.helpers import calculate_discounted_max_cost
    from routstr.payment.models import Pricing

    mock_pricing = Pricing(
        prompt=0.01,
        completion=0.02,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
        max_prompt_cost=250.0,
        max_completion_cost=250.0,
    )
    mock_model = Mock()
    mock_model.sats_pricing = mock_pricing

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 10):
            body = {
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "test"}],
            }
            cost = await calculate_discounted_max_cost(
                max_cost_for_model=500000,
                body=body,
                model_obj=mock_model,
            )
            assert cost > 0
            assert cost <= 500000


async def test_calculate_discounted_max_cost_with_max_tokens() -> None:
    """Test discounted cost calculation with max_tokens specified."""
    from routstr.payment.helpers import calculate_discounted_max_cost
    from routstr.payment.models import Pricing

    mock_pricing = Pricing(
        prompt=0.01,
        completion=0.02,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
        max_prompt_cost=250.0,
        max_completion_cost=250.0,
    )
    mock_model = Mock()
    mock_model.sats_pricing = mock_pricing

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 10):
            body = {
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 100,
            }
            cost = await calculate_discounted_max_cost(
                max_cost_for_model=500000,
                body=body,
                model_obj=mock_model,
            )
            assert cost > 0


async def test_calculate_discounted_max_cost_fixed_pricing() -> None:
    """Test that fixed pricing mode returns original max cost."""
    from routstr.payment.helpers import calculate_discounted_max_cost

    with patch.object(settings, "fixed_pricing", True):
        body = {"model": "gpt-4", "messages": []}
        cost = await calculate_discounted_max_cost(
            max_cost_for_model=500000,
            body=body,
            model_obj=None,
        )
        assert cost == 500000


async def test_calculate_discounted_max_cost_no_pricing() -> None:
    """Test discounted cost when model has no pricing."""
    from routstr.payment.helpers import calculate_discounted_max_cost

    mock_model = Mock()
    mock_model.sats_pricing = None

    with patch.object(settings, "fixed_pricing", False):
        body = {"model": "gpt-4", "messages": []}
        cost = await calculate_discounted_max_cost(
            max_cost_for_model=500000,
            body=body,
            model_obj=mock_model,
        )
        assert cost == 500000


def test_check_token_balance_with_valid_api_key() -> None:
    """Test check_token_balance with valid API key."""
    from routstr.payment.helpers import check_token_balance

    headers = {"authorization": "Bearer sk-test123"}
    body = {"model": "gpt-4"}
    
    check_token_balance(headers, body, 1000)


def test_check_token_balance_missing_token() -> None:
    """Test check_token_balance with no auth token."""
    import pytest
    from fastapi import HTTPException
    from routstr.payment.helpers import check_token_balance

    headers = {}
    body = {"model": "gpt-4"}
    
    with pytest.raises(HTTPException) as exc_info:
        check_token_balance(headers, body, 1000)
    
    assert exc_info.value.status_code == 401


def test_check_token_balance_empty_token() -> None:
    """Test check_token_balance with empty token."""
    import pytest
    from fastapi import HTTPException
    from routstr.payment.helpers import check_token_balance

    headers = {"authorization": "Bearer "}
    body = {"model": "gpt-4"}
    
    with pytest.raises(HTTPException) as exc_info:
        check_token_balance(headers, body, 1000)
    
    assert exc_info.value.status_code == 401


def test_estimate_tokens_basic() -> None:
    """Test basic token estimation."""
    from routstr.payment.helpers import estimate_tokens

    messages = [
        {"role": "user", "content": "Hello, how are you?"},
        {"role": "assistant", "content": "I'm doing well, thank you!"},
    ]
    
    tokens = estimate_tokens(messages)
    assert tokens > 0


def test_estimate_tokens_with_list_content() -> None:
    """Test token estimation with list content."""
    from routstr.payment.helpers import estimate_tokens

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Hello, how are you?"},
                {"type": "text", "text": "What's your name?"},
            ],
        }
    ]
    
    tokens = estimate_tokens(messages)
    assert tokens > 0


def test_estimate_tokens_empty_messages() -> None:
    """Test token estimation with empty messages."""
    from routstr.payment.helpers import estimate_tokens

    messages: list = []
    
    tokens = estimate_tokens(messages)
    assert tokens == 0


def test_create_error_response_basic() -> None:
    """Test creating a basic error response."""
    from fastapi import Request
    from routstr.payment.helpers import create_error_response

    mock_request = Mock(spec=Request)
    mock_request.state = Mock()
    mock_request.state.request_id = "test-123"
    
    response = create_error_response(
        error_type="test_error",
        message="Test error message",
        status_code=400,
        request=mock_request,
    )
    
    assert response.status_code == 400
    assert response.media_type == "application/json"


def test_create_error_response_with_token() -> None:
    """Test creating error response with token header."""
    from fastapi import Request
    from routstr.payment.helpers import create_error_response

    mock_request = Mock(spec=Request)
    mock_request.state = Mock()
    mock_request.state.request_id = "test-123"
    
    response = create_error_response(
        error_type="test_error",
        message="Test error",
        status_code=402,
        request=mock_request,
        token="test_token",
    )
    
    assert response.status_code == 402
    assert "X-Cashu" in response.headers
    assert response.headers["X-Cashu"] == "test_token"


def test_create_error_response_no_request_id() -> None:
    """Test creating error response when request has no ID."""
    from fastapi import Request
    from routstr.payment.helpers import create_error_response

    mock_request = Mock(spec=Request)
    mock_request.state = Mock(spec=[])
    
    response = create_error_response(
        error_type="test_error",
        message="Test error",
        status_code=500,
        request=mock_request,
    )
    
    assert response.status_code == 500
