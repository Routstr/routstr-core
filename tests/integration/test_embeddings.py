import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_embeddings_endpoint(authenticated_client: AsyncClient) -> None:
    """Test the embeddings endpoint proxy functionality"""

    test_payload = {
        "model": "text-embedding-ada-002",
        "input": "The quick brown fox",
    }

    mock_response_data = {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": [0.0023, -0.0012, 0.0045], "index": 0}
        ],
        "model": "text-embedding-ada-002",
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }

    with patch("httpx.AsyncClient.send") as mock_send:
        # Create a proper async generator for iter_bytes
        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield json.dumps(mock_response_data).encode()

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(mock_response_data)
        # Use MagicMock for synchronous .json() method
        mock_response.json = MagicMock(return_value=mock_response_data)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        # Make POST request to embeddings endpoint
        response = await authenticated_client.post("/v1/embeddings", json=test_payload)

        assert response.status_code == 200
        response_data = response.json()
        assert response_data["object"] == "list"
        assert len(response_data["data"]) == 1
        assert response_data["data"][0]["object"] == "embedding"

        # Verify request was forwarded
        mock_send.assert_called_once()
        forwarded_request = mock_send.call_args[0][0]
        # Verify the path ends with embeddings
        # Note: forwarded path might be full URL
        assert str(forwarded_request.url).endswith("embeddings")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_case_insensitivity(authenticated_client: AsyncClient) -> None:
    """Test that model lookups are case insensitive"""

    # We'll use a mixed-case model ID that should match the lowercase one in the system
    # We assume 'gpt-3.5-turbo' is available in the mock env/database

    test_payload = {
        "model": "GPT-3.5-TURBO",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    with patch("httpx.AsyncClient.send") as mock_send:
        mock_response_data = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "choices": [{"message": {"content": "Hi"}}],
            "usage": {"total_tokens": 10},
        }

        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield json.dumps(mock_response_data).encode()

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(mock_response_data)
        mock_response.json = MagicMock(return_value=mock_response_data)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        response = await authenticated_client.post(
            "/v1/chat/completions", json=test_payload
        )

        assert response.status_code == 200
