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
    """Test calculate_discounted_max_cost with basic scenario."""
    from routstr.payment.helpers import calculate_discounted_max_cost
    from routstr.payment.models import Pricing

    body = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
    }

    mock_pricing = Pricing(
        prompt=0.03,
        completion=0.06,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=100.0,
        max_prompt_cost=1000.0,
        max_completion_cost=2000.0,
    )
    mock_model = Mock()
    mock_model.sats_pricing = mock_pricing

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 0):
            result = await calculate_discounted_max_cost(
                100000, body, model_obj=mock_model
            )
            assert isinstance(result, int)
            assert result >= 0


async def test_calculate_discounted_max_cost_with_images() -> None:
    """Test calculate_discounted_max_cost with images in messages."""
    from routstr.payment.helpers import calculate_discounted_max_cost
    from routstr.payment.models import Pricing

    body = {
        "model": "gpt-4-vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What's in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
                        },
                    },
                ],
            }
        ],
        "max_tokens": 100,
    }

    mock_pricing = Pricing(
        prompt=0.03,
        completion=0.06,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=100.0,
        max_prompt_cost=1000.0,
        max_completion_cost=2000.0,
    )
    mock_model = Mock()
    mock_model.sats_pricing = mock_pricing

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 0):
            result = await calculate_discounted_max_cost(
                100000, body, model_obj=mock_model
            )
            assert isinstance(result, int)
            assert result >= 0


async def test_calculate_discounted_max_cost_edge_cases() -> None:
    """Test calculate_discounted_max_cost edge cases."""
    from routstr.payment.helpers import calculate_discounted_max_cost

    with patch.object(settings, "fixed_pricing", True):
        result = await calculate_discounted_max_cost(100000, {}, model_obj=None)
        assert result == 100000

    with patch.object(settings, "fixed_pricing", False):
        result = await calculate_discounted_max_cost(100000, {}, model_obj=None)
        assert result == 100000


def test_estimate_tokens() -> None:
    """Test estimate_tokens function."""
    from routstr.payment.helpers import estimate_tokens

    messages = [
        {"role": "user", "content": "Hello, world!"},
        {"role": "assistant", "content": "Hi there!"},
    ]

    tokens = estimate_tokens(messages)
    assert isinstance(tokens, int)
    assert tokens > 0


def test_estimate_tokens_with_list_content() -> None:
    """Test estimate_tokens with list content."""
    from routstr.payment.helpers import estimate_tokens

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": "World"},
            ],
        }
    ]

    tokens = estimate_tokens(messages)
    assert isinstance(tokens, int)
    assert tokens > 0


def test_estimate_tokens_empty() -> None:
    """Test estimate_tokens with empty messages."""
    from routstr.payment.helpers import estimate_tokens

    tokens = estimate_tokens([])
    assert tokens == 0


def test_check_token_balance() -> None:
    """Test check_token_balance function."""
    from fastapi import HTTPException
    from routstr.payment.helpers import check_token_balance

    headers = {"Authorization": "Bearer sk-test-key"}
    body = {"model": "gpt-4"}
    max_cost = 1000

    check_token_balance(headers, body, max_cost)


def test_check_token_balance_insufficient() -> None:
    """Test check_token_balance with insufficient balance."""
    from fastapi import HTTPException
    from routstr.payment.helpers import check_token_balance
    from routstr.wallet import deserialize_token_from_string

    token_string = "cashuAeyJ0b2tlbiI6W3sibWludCI6Imh0dHA6Ly9sb2NhbGhvc3Q6MzMzOCIsInByb29mcyI6W3siaWQiOiJ0ZXN0Iiwic2VjcmV0IjoiMTIzIiwgIkMiOiIwMjM0NTY3ODkwYWJjZGVmMTIzNDU2Nzg5MGFiY2RlZjEyMzQ1Njc4OTBhYmNkZWYxMjM0NTY3ODkwYWJjZGVmMTIiIn1dfV0="

    headers = {"Authorization": f"Bearer {token_string}"}
    body = {"model": "gpt-4"}
    max_cost = 1000000000

    try:
        check_token_balance(headers, body, max_cost)
        assert False, "Should have raised HTTPException"
    except HTTPException as e:
        assert e.status_code == 413


def test_check_token_balance_no_auth() -> None:
    """Test check_token_balance with no authentication."""
    from fastapi import HTTPException
    from routstr.payment.helpers import check_token_balance

    headers = {}
    body = {"model": "gpt-4"}
    max_cost = 1000

    try:
        check_token_balance(headers, body, max_cost)
        assert False, "Should have raised HTTPException"
    except HTTPException as e:
        assert e.status_code == 401


def test_create_error_response() -> None:
    """Test create_error_response function."""
    from fastapi import Request
    from fastapi.testclient import TestClient
    from routstr.payment.helpers import create_error_response

    app = TestClient(lambda: None)
    request = Request({"type": "http", "method": "GET", "url": "/test"})

    response = create_error_response(
        error_type="test_error",
        message="Test error message",
        status_code=400,
        request=request,
    )

    assert response.status_code == 400
    assert response.media_type == "application/json"


def test_create_error_response_with_token() -> None:
    """Test create_error_response with token."""
    from fastapi import Request
    from fastapi.testclient import TestClient
    from routstr.payment.helpers import create_error_response

    app = TestClient(lambda: None)
    request = Request({"type": "http", "method": "GET", "url": "/test"})

    response = create_error_response(
        error_type="test_error",
        message="Test error message",
        status_code=400,
        request=request,
        token="test-token",
    )

    assert response.status_code == 400
    assert "X-Cashu" in response.headers
