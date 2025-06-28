import json
import os
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from router.db import ApiKey, AsyncSession


@pytest_asyncio.fixture
async def api_key_with_balance(test_session: AsyncSession) -> ApiKey:
    """Create an API key with sufficient balance."""
    unique_id = str(uuid.uuid4())[:8]
    key = ApiKey(
        hashed_key=f"test-hashed-key-{unique_id}",
        balance=10000000,  # 10,000 sats in msats
        refund_address=None,
        total_spent=0,
        total_requests=0,
    )
    test_session.add(key)
    await test_session.commit()
    await test_session.refresh(key)
    return key


@pytest.mark.asyncio
async def test_proxy_requires_authentication(async_client: AsyncClient) -> None:
    """Test that proxy endpoints require authentication."""
    response = await async_client.post("/v1/chat/completions")

    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"


@pytest.mark.asyncio
async def test_proxy_empty_bearer_token(async_client: AsyncClient) -> None:
    """Test that proxy endpoints return structured error for empty bearer token."""
    response = await async_client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer "}
    )

    assert response.status_code == 401
    assert (
        "API key or Cashu token required"
        in response.json()["detail"]["error"]["message"]
    )


@pytest.mark.asyncio
async def test_proxy_with_insufficient_balance(
    async_client: AsyncClient, test_session: AsyncSession
) -> None:
    """Test proxy request with insufficient balance."""
    # Create key with minimal balance
    unique_id = str(uuid.uuid4())[:8]
    key = ApiKey(
        hashed_key=f"low-balance-key-{unique_id}",
        balance=100,  # Only 0.1 sats
        refund_address=None,
        total_spent=0,
        total_requests=0,
    )
    test_session.add(key)
    await test_session.commit()

    # Mock the models.json check
    with patch("os.path.exists", return_value=False):
        response = await async_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer sk-{key.hashed_key}"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == 402
    assert "Insufficient balance" in response.json()["detail"]["error"]["message"]


@pytest.mark.asyncio
async def test_proxy_invalid_json_body(
    async_client: AsyncClient, api_key_with_balance: ApiKey
) -> None:
    """Test proxy request with invalid JSON body."""
    response = await async_client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer sk-{api_key_with_balance.hashed_key}",
            "Content-Type": "application/json",
        },
        content=b'{"invalid": json",}',  # Invalid JSON
    )

    assert response.status_code == 400
    error_data = response.json()
    assert error_data["error"]["type"] == "invalid_request_error"
    assert error_data["error"]["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_proxy_successful_request_mock(
    async_client: AsyncClient, api_key_with_balance: ApiKey, test_session: AsyncSession
) -> None:
    """Test successful proxy request with mocked upstream."""
    mock_response_data = {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": 1677652288,
        "model": "gpt-4",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello! How can I help you?",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 10, "total_tokens": 19},
    }

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Create a mock response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.aread = AsyncMock(
            return_value=json.dumps(mock_response_data).encode()
        )
        mock_response.aiter_bytes = AsyncMock()
        mock_response.aclose = AsyncMock()

        mock_client.send = AsyncMock(return_value=mock_response)
        mock_client.build_request = AsyncMock()
        mock_client.aclose = AsyncMock()

        # Also mock the models.json check and pay_out
        with patch("os.path.exists", return_value=False):
            with patch("router.cashu.pay_out") as mock_payout:
                mock_payout.return_value = None

                response = await async_client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer sk-{api_key_with_balance.hashed_key}"
                    },
                    json={
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": "Hello"}],
                    },
                )

        assert response.status_code == 200
        response_json = response.json()

        # Verify the response includes the original data plus cost
        assert response_json["id"] == "chatcmpl-123"
        assert "cost" in response_json
        assert response_json["cost"]["total_msats"] >= 0

        # Verify balance was deducted
        await test_session.refresh(api_key_with_balance)
        assert api_key_with_balance.balance < 10000000
        assert api_key_with_balance.total_requests == 1


@pytest.mark.asyncio
async def test_proxy_streaming_response(
    async_client: AsyncClient, api_key_with_balance: ApiKey
) -> None:
    """Test proxy request with streaming response."""
    # Mock SSE stream chunks
    stream_chunks = [
        b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-4","choices":[{"delta":{"content":"Hello"},"index":0}]}\n\n',
        b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-4","choices":[{"delta":{"content":" there!"},"index":0}]}\n\n',
        b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-4","choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":3,"total_tokens":12}}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def mock_aiter_bytes() -> AsyncGenerator[bytes, None]:
        for chunk in stream_chunks:
            yield chunk

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/event-stream"}
        mock_response.aiter_bytes = lambda: mock_aiter_bytes()
        mock_response.aclose = AsyncMock()

        mock_client.send = AsyncMock(return_value=mock_response)
        mock_client.build_request = AsyncMock()
        mock_client.aclose = AsyncMock()

        with patch("os.path.exists", return_value=False):
            with patch("router.cashu.pay_out") as mock_payout:
                mock_payout.return_value = None

                response = await async_client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer sk-{api_key_with_balance.hashed_key}"
                    },
                    json={
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": "Hello"}],
                        "stream": True,
                    },
                )

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream"


@pytest.mark.asyncio
async def test_proxy_handles_upstream_errors(
    async_client: AsyncClient, api_key_with_balance: ApiKey
) -> None:
    """Test proxy handles upstream connection errors gracefully."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Simulate connection error
        mock_client.send.side_effect = Exception("Connection refused")
        mock_client.build_request = AsyncMock()
        mock_client.aclose = AsyncMock()

        with patch("os.path.exists", return_value=False):
            response = await async_client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer sk-{api_key_with_balance.hashed_key}"
                },
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 500
        error_data = response.json()
        assert error_data["error"]["type"] == "internal_error"
        assert error_data["error"]["message"] == "An unexpected server error occurred"


@pytest.mark.asyncio
async def test_proxy_with_model_based_pricing(
    async_client: AsyncClient, test_session: AsyncSession
) -> None:
    """Test proxy with model-based pricing enabled."""
    # Create API key with sufficient balance
    unique_id = str(uuid.uuid4())[:8]
    key = ApiKey(
        hashed_key=f"model-pricing-key-{unique_id}",
        balance=10000000,  # 10,000 sats
        refund_address=None,
        total_spent=0,
        total_requests=0,
    )
    test_session.add(key)
    await test_session.commit()

    with patch.dict(os.environ, {"MODEL_BASED_PRICING": "true"}):
        with patch("os.path.exists", return_value=True):
            # Mock a model with pricing
            from router.models import MODELS, Architecture, Model, Pricing, TopProvider

            test_model = Model(
                id="gpt-4",
                name="GPT-4",
                created=1680000000,
                description="Test model",
                context_length=8192,
                architecture=Architecture(
                    modality="text",
                    input_modalities=["text"],
                    output_modalities=["text"],
                    tokenizer="cl100k_base",
                    instruct_type="none",
                ),
                pricing=Pricing(
                    prompt=0.03,
                    completion=0.06,
                    request=0.001,
                    image=0.0,
                    web_search=0.0,
                    internal_reasoning=0.0,
                ),
                sats_pricing=Pricing(
                    prompt=300,  # 300 sats per 1k tokens
                    completion=600,
                    request=10,
                    image=0.0,
                    web_search=0.0,
                    internal_reasoning=0.0,
                    max_cost=5000,  # 5000 sats max
                ),
                top_provider=TopProvider(
                    context_length=8192, max_completion_tokens=4096, is_moderated=False
                ),
            )

            # Temporarily replace models
            original_models = MODELS[:]
            MODELS.clear()
            MODELS.append(test_model)

            # Mock the upstream HTTP client
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                # Create a mock response
                mock_response = AsyncMock()
                mock_response.status_code = 200
                mock_response.headers = {"content-type": "application/json"}
                mock_response.aread = AsyncMock(
                    return_value=b'{"id": "test", "model": "gpt-4"}'
                )
                mock_response.aiter_bytes = AsyncMock()
                mock_response.aclose = AsyncMock()

                mock_client.send = AsyncMock(return_value=mock_response)
                mock_client.build_request = AsyncMock()
                mock_client.aclose = AsyncMock()

                try:
                    response = await async_client.post(
                        "/v1/chat/completions",
                        headers={"Authorization": f"Bearer sk-{key.hashed_key}"},
                        json={
                            "model": "gpt-4",
                            "messages": [{"role": "user", "content": "Hello"}],
                        },
                    )

                    # Should succeed because balance (10,000 sats) > max_cost (5000 sats)
                    assert response.status_code == 200

                finally:
                    MODELS.clear()
                    MODELS.extend(original_models)
