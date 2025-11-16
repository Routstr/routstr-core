import os
from typing import Any
from unittest.mock import AsyncMock, Mock, MagicMock, patch

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

import pytest
from fastapi import Request
from fastapi.responses import Response

from routstr.core.settings import settings  # noqa: E402
from routstr.payment.helpers import (  # noqa: E402
    calculate_discounted_max_cost,
    check_token_balance,
    create_error_response,
    estimate_tokens,
    get_max_cost_for_model,
)


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
    from routstr.payment.models import Pricing

    mock_model = Mock()
    mock_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
        max_prompt_cost=1000.0,
        max_completion_cost=2000.0,
    )
    mock_model.sats_pricing = mock_pricing

    body = {
        "model": "gpt-4",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
    }

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 10):
            discounted = await calculate_discounted_max_cost(
                500000, body, model_obj=mock_model
            )
            assert discounted >= 0
            assert discounted <= 500000


async def test_calculate_discounted_max_cost_with_images() -> None:
    from routstr.payment.helpers import estimate_image_tokens_in_messages
    from routstr.payment.models import Pricing

    mock_model = Mock()
    mock_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
        max_prompt_cost=1000.0,
        max_completion_cost=2000.0,
    )
    mock_model.sats_pricing = mock_pricing

    body = {
        "model": "gpt-4",
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

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 10):
            discounted = await calculate_discounted_max_cost(
                500000, body, model_obj=mock_model
            )
            assert discounted >= 0
            assert discounted <= 500000


async def test_calculate_discounted_max_cost_fixed_pricing() -> None:
    body = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}

    with patch.object(settings, "fixed_pricing", True):
        discounted = await calculate_discounted_max_cost(500000, body, model_obj=None)
        assert discounted == 500000


async def test_calculate_discounted_max_cost_no_model_pricing() -> None:
    body = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}

    with patch.object(settings, "fixed_pricing", False):
        discounted = await calculate_discounted_max_cost(500000, body, model_obj=None)
        assert discounted == 500000


async def test_calculate_discounted_max_cost_edge_cases() -> None:
    from routstr.payment.models import Pricing

    mock_model = Mock()
    mock_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        request=0.0,
        image=0.0,
        web_search=0.0,
        internal_reasoning=0.0,
        max_cost=500.0,
        max_prompt_cost=1000.0,
        max_completion_cost=2000.0,
    )
    mock_model.sats_pricing = mock_pricing

    with patch.object(settings, "fixed_pricing", False):
        with patch.object(settings, "tolerance_percentage", 0):
            body_empty = {"model": "gpt-4", "messages": []}
            discounted = await calculate_discounted_max_cost(
                500000, body_empty, model_obj=mock_model
            )
            assert discounted >= 0

            body_no_max_tokens = {
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hello"}],
            }
            discounted = await calculate_discounted_max_cost(
                500000, body_no_max_tokens, model_obj=mock_model
            )
            assert discounted >= 0


def test_check_token_balance_with_api_key() -> None:
    headers = {"Authorization": "Bearer sk-test-key-123"}
    body = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}

    check_token_balance(headers, body, 1000)


def test_check_token_balance_with_x_cashu() -> None:
    headers = {"x-cashu": "cashuA...test"}
    body = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}

    with patch("routstr.payment.helpers.deserialize_token_from_string") as mock_deserialize:
        mock_token = Mock()
        mock_token.amount = 5000
        mock_token.unit = "msat"
        mock_deserialize.return_value = mock_token

        check_token_balance(headers, body, 1000)


def test_check_token_balance_insufficient_balance() -> None:
    import pytest
    from fastapi import HTTPException

    headers = {"x-cashu": "cashuA...test"}
    body = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}

    with patch("routstr.payment.helpers.deserialize_token_from_string") as mock_deserialize:
        mock_token = Mock()
        mock_token.amount = 500
        mock_token.unit = "msat"
        mock_deserialize.return_value = mock_token

        with pytest.raises(HTTPException) as exc_info:
            check_token_balance(headers, body, 1000)
        assert exc_info.value.status_code == 413


def test_check_token_balance_no_auth() -> None:
    import pytest
    from fastapi import HTTPException

    headers = {}
    body = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}

    with pytest.raises(HTTPException) as exc_info:
        check_token_balance(headers, body, 1000)
    assert exc_info.value.status_code == 401


def test_estimate_tokens_simple() -> None:
    messages = [{"role": "user", "content": "Hello world"}]
    tokens = estimate_tokens(messages)
    assert tokens > 0
    assert tokens <= len("Hello world") // 3 + 1


def test_estimate_tokens_multiple_messages() -> None:
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    tokens = estimate_tokens(messages)
    assert tokens > 0


def test_estimate_tokens_with_list_content() -> None:
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
    assert tokens > 0


def test_estimate_tokens_empty() -> None:
    messages = []
    tokens = estimate_tokens(messages)
    assert tokens == 0


def test_create_error_response() -> None:
    mock_request = MagicMock(spec=Request)
    mock_request.state.request_id = "test-request-123"

    response = create_error_response(
        error_type="test_error",
        message="Test error message",
        status_code=400,
        request=mock_request,
        token="test-token",
    )

    assert isinstance(response, Response)
    assert response.status_code == 400
    assert "test-token" in response.headers.get("X-Cashu", "")


def test_create_error_response_no_token() -> None:
    mock_request = MagicMock(spec=Request)
    mock_request.state.request_id = "test-request-456"

    response = create_error_response(
        error_type="test_error",
        message="Test error message",
        status_code=500,
        request=mock_request,
        token=None,
    )

    assert isinstance(response, Response)
    assert response.status_code == 500
    assert "X-Cashu" not in response.headers or not response.headers.get("X-Cashu")
