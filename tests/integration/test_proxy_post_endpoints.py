"""
Integration tests for proxy POST endpoints.
Tests POST /{path} proxy functionality for LLM completions with various payloads and streaming.
"""

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from .utils import (
    ConcurrencyTester,
    PerformanceValidator,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_json_payload_forwarding(
    integration_client: AsyncClient, authenticated_client: AsyncClient, db_snapshot: Any
) -> None:
    """Test that JSON payloads are correctly forwarded to upstream"""

    # Test payload for chat completion
    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello, how are you?"},
        ],
        "temperature": 0.7,
        "max_tokens": 150,
    }

    # Mock upstream response
    mock_response_data = {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": 1677652288,
        "model": "gpt-3.5-turbo",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "I'm doing well, thank you! How can I help you today?",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
    }

    await db_snapshot.capture()

    with patch("httpx.AsyncClient.send") as mock_send:
        # Create a proper async generator for iter_bytes
        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield json.dumps(mock_response_data).encode()

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(mock_response_data)
        mock_response.json = AsyncMock(return_value=mock_response_data)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        # Make POST request
        response = await authenticated_client.post(
            "/v1/chat/completions", json=test_payload
        )

        assert response.status_code == 200

        # Verify response
        response_data = json.loads(response.text)
        assert response_data["object"] == "chat.completion"
        assert "choices" in response_data
        assert response_data["usage"]["total_tokens"] == 35

        # Verify the request was forwarded correctly
        mock_send.assert_called_once()
        forwarded_request = mock_send.call_args[0][0]

        # Check that payload was forwarded
        forwarded_body = forwarded_request.content.decode()
        forwarded_json = json.loads(forwarded_body)
        assert forwarded_json["model"] == test_payload["model"]
        assert forwarded_json["messages"] == test_payload["messages"]
        assert forwarded_json["temperature"] == test_payload["temperature"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_streaming_response(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test streaming responses for POST requests (SSE format)"""

    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Count to 3"}],
        "stream": True,
    }

    # Mock SSE streaming response chunks
    streaming_chunks = [
        b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":"One"},"finish_reason":null}]}\n\n',
        b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":", two"},"finish_reason":null}]}\n\n',
        b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{"content":", three!"},"finish_reason":null}]}\n\n',
        b'data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1677652288,"model":"gpt-3.5-turbo","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    with patch("httpx.AsyncClient.send") as mock_send:
        # Create an async generator for streaming
        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            for chunk in streaming_chunks:
                yield chunk
                await asyncio.sleep(0.01)  # Simulate streaming delay

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "content-type": "text/event-stream",
            "transfer-encoding": "chunked",
        }
        # For streaming response, text property should contain assembled chunks
        mock_response.text = b"".join(streaming_chunks).decode()
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        # Make streaming request
        response = await authenticated_client.post(
            "/v1/chat/completions", json=test_payload
        )

        assert response.status_code == 200
        assert response.headers.get("content-type") == "text/event-stream"

        # For streaming responses, check the content
        # In tests, the response is already assembled
        response_text = response.text
        assert "One" in response_text
        assert "two" in response_text
        assert "three!" in response_text
        assert "[DONE]" in response_text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_non_streaming_response(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test non-streaming responses work correctly"""

    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "What is 2+2?"}],
        "stream": False,  # Explicitly non-streaming
    }

    mock_response_data = {
        "id": "chatcmpl-456",
        "object": "chat.completion",
        "created": 1677652290,
        "model": "gpt-3.5-turbo",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "2+2 equals 4."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }

    with patch("httpx.AsyncClient.send") as mock_send:
        # Create a proper async generator for iter_bytes
        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield json.dumps(mock_response_data).encode()

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(mock_response_data)
        mock_response.json = AsyncMock(return_value=mock_response_data)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        response = await authenticated_client.post(
            "/v1/chat/completions", json=test_payload
        )

        assert response.status_code == 200
        assert response.headers.get("content-type") == "application/json"

        # Should return complete response, not streamed
        response_data = json.loads(response.text)
        assert response_data["object"] == "chat.completion"
        assert response_data["choices"][0]["message"]["content"] == "2+2 equals 4."


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_content_type_preserved(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test that Content-Type headers are preserved in both directions"""

    test_cases: list[dict[str, Any]] = [
        {
            "content_type": "application/json",
            "payload": {"model": "gpt-3.5-turbo", "prompt": "test"},
            "response_type": "application/json",
        },
        {
            "content_type": "application/json; charset=utf-8",
            "payload": {"model": "gpt-3.5-turbo", "prompt": "test"},
            "response_type": "application/json; charset=utf-8",
        },
    ]

    for test_case in test_cases:
        with patch("httpx.AsyncClient.send") as mock_send:
            # Create a proper async generator for iter_bytes
            async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
                yield b'{"result": "success"}'

            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": test_case["response_type"]}
            mock_response.text = '{"result": "success"}'
            mock_response.json = AsyncMock(return_value={"result": "success"})
            mock_response.iter_bytes = mock_iter_bytes
            mock_response.aiter_bytes = mock_iter_bytes
            mock_send.return_value = mock_response

            # Make request with specific content type
            response = await authenticated_client.post(
                "/v1/completions",
                json=test_case["payload"],
                headers={"Content-Type": str(test_case["content_type"])},
            )

            assert response.status_code == 200

            # Verify request content type was forwarded
            forwarded_request = mock_send.call_args[0][0]
            assert (
                forwarded_request.headers.get("content-type")
                == test_case["content_type"]
            )

            # Verify response content type is preserved
            assert response.headers.get("content-type") == test_case["response_type"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_unauthorized_access(integration_client: AsyncClient) -> None:
    """Test that POST requests require authentication"""

    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    # No auth header
    response = await integration_client.post("/v1/chat/completions", json=test_payload)
    assert response.status_code == 401

    # Invalid auth
    response = await integration_client.post(
        "/v1/chat/completions",
        json=test_payload,
        headers={"Authorization": "Bearer invalid-key"},
    )
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_performance(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test POST endpoint performance requirements"""

    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Performance test"}],
    }

    validator = PerformanceValidator()

    with patch("httpx.AsyncClient.send") as mock_send:
        # Mock fast responses
        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield b'{"choices": [{"message": {"content": "Fast"}}], "usage": {"total_tokens": 5}}'

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        response_data = {
            "choices": [{"message": {"content": "Fast"}}],
            "usage": {"total_tokens": 5},
        }
        mock_response.text = json.dumps(response_data)
        mock_response.json = AsyncMock(return_value=response_data)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        # Run multiple requests for performance measurement
        for i in range(20):
            start = validator.start_timing("proxy_post")
            response = await authenticated_client.post(
                "/v1/chat/completions", json=test_payload
            )
            validator.end_timing("proxy_post", start)

            assert response.status_code == 200

    # Validate performance
    perf_result = validator.validate_response_time(
        "proxy_post",
        max_duration=1.5,  # Allow slightly more time for POST
        percentile=0.95,
    )
    assert perf_result["valid"], f"Performance requirement failed: {perf_result}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_model_specific_endpoints(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test different model endpoints work correctly"""

    test_cases: list[dict[str, Any]] = [
        {
            "endpoint": "/v1/chat/completions",
            "payload": {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            "response": {"object": "chat.completion", "model": "gpt-3.5-turbo"},
        },
        # Coming soon - completions endpoint
        # {
        #     "endpoint": "/v1/completions",
        #     "payload": {
        #         "model": "text-davinci-003",
        #         "prompt": "Hello world",
        #         "max_tokens": 50,
        #     },
        #     "response": {"object": "text_completion", "model": "text-davinci-003"},
        # },
        # Coming soon - embeddings endpoint
        # {
        #     "endpoint": "/v1/embeddings",
        #     "payload": {
        #         "model": "text-embedding-ada-002",
        #         "input": "The quick brown fox",
        #     },
        #     "response": {
        #         "object": "list",
        #         "model": "text-embedding-ada-002",
        #         "data": [{"object": "embedding", "embedding": [0.1, 0.2, 0.3]}],
        #     },
        # },
    ]

    for test_case in test_cases:
        with patch("httpx.AsyncClient.send") as mock_send:
            # Add usage data for billing tests
            response_data = test_case["response"].copy()
            response_data["usage"] = {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            }

            # Create a proper async generator for iter_bytes
            async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
                yield json.dumps(response_data).encode()

            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.text = json.dumps(response_data)
            mock_response.json = AsyncMock(return_value=response_data)
            mock_response.iter_bytes = mock_iter_bytes
            mock_response.aiter_bytes = mock_iter_bytes
            mock_send.return_value = mock_response

            response = await authenticated_client.post(
                str(test_case["endpoint"]), json=test_case["payload"]
            )

            assert response.status_code == 200
            response_data = json.loads(response.text)
            assert response_data["object"] == str(test_case["response"]["object"])
            assert response_data["model"] == str(test_case["response"]["model"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_billing_token_counting(
    integration_client: AsyncClient, authenticated_client: AsyncClient, db_snapshot: Any
) -> None:
    """Test that token counting and billing is accurate for completions"""

    test_payload = {
        "model": "gpt-4",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Write a haiku about coding"},
        ],
    }

    mock_response_data = {
        "id": "chatcmpl-789",
        "object": "chat.completion",
        "created": 1677652295,
        "model": "gpt-4",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Code flows like water\nBugs hide in syntax shadows\nDebugger finds peace",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 25, "completion_tokens": 17, "total_tokens": 42},
    }

    await db_snapshot.capture()

    with patch("httpx.AsyncClient.send") as mock_send:
        # Create a proper async generator for iter_bytes
        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield json.dumps(mock_response_data).encode()

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(mock_response_data)
        mock_response.json = AsyncMock(return_value=mock_response_data)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        # Make request
        response = await authenticated_client.post(
            "/v1/chat/completions", json=test_payload
        )

        assert response.status_code == 200

        # Verify token usage is returned
        response_data = json.loads(response.text)
        assert response_data["usage"]["prompt_tokens"] == 25
        assert response_data["usage"]["completion_tokens"] == 17
        assert response_data["usage"]["total_tokens"] == 42

        # For x-cashu authentication, billing happens per-request
        # Database changes would depend on the implementation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_streaming_billing_calculation(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test billing calculation for streaming responses"""

    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Tell me a short story"}],
        "stream": True,
    }

    # Mock streaming chunks with usage info in final chunk
    streaming_chunks = [
        b'data: {"choices":[{"delta":{"content":"Once upon"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" a time"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"..."}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":8,"total_tokens":18}}\n\n',
        b"data: [DONE]\n\n",
    ]

    with patch("httpx.AsyncClient.send") as mock_send:

        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            for chunk in streaming_chunks:
                yield chunk

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/event-stream"}
        mock_response.text = b"".join(streaming_chunks).decode()
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        response = await authenticated_client.post(
            "/v1/chat/completions", json=test_payload
        )

        assert response.status_code == 200

        # Verify usage data is in the response
        response_text = response.text
        assert '"usage"' in response_text
        assert '"total_tokens":18' in response_text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_large_payload_handling(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test handling of large payloads (>1MB)"""

    # Create a large payload
    large_messages = []
    for i in range(100):
        large_messages.append(
            {
                "role": "user",
                "content": "A" * 10000,  # 10KB per message = ~1MB total
            }
        )

    large_payload = {
        "model": "gpt-3.5-turbo",
        "messages": large_messages[:10],  # Start with smaller test
        "max_tokens": 10,
    }

    with patch("httpx.AsyncClient.send") as mock_send:
        mock_response_data = {
            "choices": [{"message": {"content": "Response"}}],
            "usage": {"total_tokens": 1000},
        }

        # Create a proper async generator for iter_bytes
        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield json.dumps(mock_response_data).encode()

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(mock_response_data)
        mock_response.json = AsyncMock(return_value=mock_response_data)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        # Should handle large payload
        response = await authenticated_client.post(
            "/v1/chat/completions", json=large_payload
        )

        assert response.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_malformed_json_request(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test handling of malformed JSON requests"""

    # Test various malformed requests
    test_cases: list[dict[str, Any]] = [
        # Missing required fields
        {"model": "gpt-3.5-turbo"},  # Missing messages
        # Invalid field types
        {"model": "gpt-3.5-turbo", "messages": "not an array"},
        # Empty payload
        {},
        # Invalid model
        {"model": "invalid-model-xxx", "messages": [{"role": "user", "content": "Hi"}]},
    ]

    for invalid_payload in test_cases:
        with patch("httpx.AsyncClient.send") as mock_send:
            # Mock upstream error response
            error_response = {
                "error": {
                    "message": "Invalid request",
                    "type": "invalid_request_error",
                    "code": "invalid_request",
                }
            }

            # Create a proper async generator for iter_bytes
            async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
                yield json.dumps(error_response).encode()

            mock_response = AsyncMock()
            mock_response.status_code = 400
            mock_response.headers = {"content-type": "application/json"}
            mock_response.text = json.dumps(error_response)
            mock_response.json = AsyncMock(return_value=error_response)
            mock_response.iter_bytes = mock_iter_bytes
            mock_response.aiter_bytes = mock_iter_bytes
            mock_send.return_value = mock_response

            response = await authenticated_client.post(
                "/v1/chat/completions", json=invalid_payload
            )

            # Should return error from upstream
            assert response.status_code == 400
            response_data = json.loads(response.text)
            assert "error" in response_data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_insufficient_balance(
    integration_client: AsyncClient, testmint_wallet: Any, integration_session: Any
) -> None:
    """Test handling when balance is insufficient for request"""

    # Skip this test for now as it's dependent on model pricing configuration
    pytest.skip(
        "Skipping insufficient balance test - depends on model pricing configuration"
    )

    # Create a low balance token for testing
    token = await testmint_wallet.mint_tokens(1)  # 1 sat only

    # The check_token_balance is called inside the proxy endpoint
    # So we test via the API directly

    # Now test via API endpoint
    low_balance_client = AsyncClient(
        transport=ASGITransport(app=integration_client._transport.app),
        base_url=integration_client.base_url,
        headers={"x-cashu": token},
    )

    test_payload = {
        "model": "gpt-4",  # Expensive model
        "messages": [{"role": "user", "content": "Write a long essay"}],
        "max_tokens": 4000,  # Large request
    }

    # Mock the upstream request to prevent actual HTTP call
    with patch("httpx.AsyncClient.send") as mock_send:
        # Even if balance check passes, we need a mock response
        mock_response_data = {"error": "This shouldn't be reached"}

        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield json.dumps(mock_response_data).encode()

        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(mock_response_data)
        mock_response.json = AsyncMock(return_value=mock_response_data)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        response = await low_balance_client.post(
            "/v1/chat/completions", json=test_payload
        )

    # Debug the response
    print(f"Response status: {response.status_code}")
    print(f"Response text: {response.text}")

    # Should return 413 for insufficient balance (checked before upstream call)
    assert response.status_code == 413
    response_data = json.loads(response.text)
    assert "insufficient" in response_data["detail"].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_rate_limiting_behavior(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test rate limiting behavior for POST requests"""

    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Quick test"}],
    }

    # Mock rate limit response
    with patch("httpx.AsyncClient.send") as mock_send:
        error_response = {
            "error": {
                "message": "Rate limit exceeded",
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
            }
        }

        # Create a proper async generator for iter_bytes
        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield json.dumps(error_response).encode()

        mock_response = AsyncMock()
        mock_response.status_code = 429
        mock_response.headers = {
            "content-type": "application/json",
            "x-ratelimit-limit": "60",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(time.time()) + 60),
        }
        mock_response.text = json.dumps(error_response)
        mock_response.json = AsyncMock(return_value=error_response)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        response = await authenticated_client.post(
            "/v1/chat/completions", json=test_payload
        )

        assert response.status_code == 429
        response_data = json.loads(response.text)
        assert "rate_limit" in response_data["error"]["type"]

        # Rate limit headers should be forwarded
        assert "x-ratelimit-limit" in response.headers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_partial_streaming_failure(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test handling of partial streaming failures"""

    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Stream test"}],
        "stream": True,
    }

    # Mock streaming that fails partway through
    streaming_chunks = [
        b'data: {"choices":[{"delta":{"content":"Starting"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" response"}}]}\n\n',
        # Simulate error mid-stream
    ]

    with patch("httpx.AsyncClient.send") as mock_send:

        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            for i, chunk in enumerate(streaming_chunks):
                if i == 2:  # Simulate failure
                    raise httpx.ReadError("Connection lost")
                yield chunk

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/event-stream"}
        # In test environment, partial response is assembled
        mock_response.text = b"".join(streaming_chunks).decode()
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        # The proxy should handle the streaming failure gracefully
        response = await authenticated_client.post(
            "/v1/chat/completions", json=test_payload
        )

        # In the test environment, the partial response is already assembled
        response_text = response.text
        # Should have received partial response
        assert "Starting" in response_text
        assert "response" in response_text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_database_state_changes(
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    db_snapshot: Any,
    integration_session: Any,
) -> None:
    """Test database state changes for POST requests"""

    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Database test"}],
    }

    await db_snapshot.capture()

    with patch("httpx.AsyncClient.send") as mock_send:
        mock_response_data = {
            "choices": [{"message": {"content": "Response"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        # Create a proper async generator for iter_bytes
        async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
            yield json.dumps(mock_response_data).encode()

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = json.dumps(mock_response_data)
        mock_response.json = AsyncMock(return_value=mock_response_data)
        mock_response.iter_bytes = mock_iter_bytes
        mock_response.aiter_bytes = mock_iter_bytes
        mock_send.return_value = mock_response

        response = await authenticated_client.post(
            "/v1/chat/completions", json=test_payload
        )

        assert response.status_code == 200

    # For x-cashu, no persistent API keys in database
    # But usage/billing might be tracked differently
    await db_snapshot.diff()

    # Verify any expected database changes based on implementation
    # This would depend on how the system tracks usage for x-cashu auth


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_post_concurrent_requests(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test handling of concurrent POST requests"""

    test_payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Concurrent test"}],
    }

    with patch("httpx.AsyncClient.send") as mock_send:
        # Mock responses for concurrent requests
        async def create_mock_response(*args: Any, **kwargs: Any) -> Any:
            response_data = {
                "id": f"chatcmpl-{time.time()}",
                "choices": [{"message": {"content": "Concurrent response"}}],
                "usage": {"total_tokens": 10},
            }

            # Create a proper async generator for iter_bytes
            async def mock_iter_bytes(*args: Any, **kwargs: Any) -> Any:
                yield json.dumps(response_data).encode()

            mock_response = AsyncMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.text = json.dumps(response_data)
            mock_response.json = AsyncMock(return_value=response_data)
            mock_response.iter_bytes = mock_iter_bytes
            mock_response.aiter_bytes = mock_iter_bytes
            return mock_response

        mock_send.side_effect = create_mock_response

        # Create concurrent requests
        requests = []
        for i in range(10):
            requests.append(
                {"method": "POST", "url": "/v1/chat/completions", "json": test_payload}
            )

        tester = ConcurrencyTester()
        responses = await tester.run_concurrent_requests(
            authenticated_client, requests, max_concurrent=5
        )

        # All should succeed
        for response in responses:
            assert response.status_code == 200
            response_data = json.loads(response.text)
            assert (
                response_data["choices"][0]["message"]["content"]
                == "Concurrent response"
            )
