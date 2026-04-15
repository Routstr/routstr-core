import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_usage_and_metadata_injection_all_endpoints(
    authenticated_client: AsyncClient,
) -> None:
    """Test that usage, cost, and metadata are injected into all completion endpoints."""

    endpoints = [
        (
            "/v1/chat/completions",
            {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            {
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "model": "gpt-3.5-turbo",
                "choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        ),
        (
            "/v1/messages",
            {
                "model": "claude-3-opus-20240229",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            },
            {
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-opus-20240229",
                "content": [{"type": "text", "text": "Hi"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ),
    ]

    for path, payload, mock_data in endpoints:
        with patch("httpx.AsyncClient.send") as mock_send:

            async def mock_iter_bytes(
                *args: object, **kwargs: object
            ) -> AsyncGenerator[bytes, None]:
                yield json.dumps(mock_data).encode()

            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.text = json.dumps(mock_data)
            mock_response.json = AsyncMock(return_value=mock_data)
            mock_response.aiter_bytes = mock_iter_bytes
            mock_send.return_value = mock_response

            response = await authenticated_client.post(path, json=payload)
            assert response.status_code == 200

            data = response.json()
            if hasattr(data, "__await__"):
                data = await data

            # Check unified usage injection
            assert "usage" in data
            assert "cost" in data["usage"]
            assert "sats_cost" in data["usage"]
            assert "remaining_balance_msats" in data["usage"]

            # Check metadata injection
            assert "metadata" in data
            assert "routstr" in data["metadata"]
            assert "cost" in data["metadata"]["routstr"]
            assert "sats_cost" in data["metadata"]["routstr"]
            assert "remaining_balance_msats" in data["metadata"]["routstr"]

            # Check legacy cost injection
            assert "cost" in data
            assert "sats_cost" in data["cost"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_streaming_usage_injection(
    authenticated_client: AsyncClient,
) -> None:
    """Test usage injection for streaming messages."""

    path = "/v1/messages"
    payload = {
        "model": "claude-3-opus-20240229",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
        "stream": True,
    }

    # Final usage chunk for Anthropic
    streaming_chunks = [
        b'event: message_start\ndata: {"type": "message_start", "message": {"id": "msg_123", "type": "message", "role": "assistant", "model": "claude-3-opus-20240229", "usage": {"input_tokens": 10, "output_tokens": 0}}}\n\n',
        b'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": null}, "usage": {"output_tokens": 5}}\n\n',
        b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]

    with patch("httpx.AsyncClient.send") as mock_send:

        async def mock_iter_bytes(
            *args: object, **kwargs: object
        ) -> AsyncGenerator[bytes, None]:
            for chunk in streaming_chunks:
                yield chunk

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/event-stream"}
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        response = await authenticated_client.post(path, json=payload)
        assert response.status_code == 200

        # In streaming, we look for the final 'event: cost' which is added by Routstr
        response_text = response.text
        if hasattr(response_text, "__await__"):
            response_text = await response_text
        lines = response_text.split("\n")

        cost_event = next(
            (line for line in lines if line.startswith('data: {"cost":')), None
        )
        assert cost_event is not None

        cost_data = json.loads(cost_event[6:])["cost"]
        assert "total_msats" in cost_data
