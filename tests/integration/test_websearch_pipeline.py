"""
FIXED WebSearch Testing Framework for BaseUpstreamProvider.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import BackgroundTasks
from fastapi.responses import Response, StreamingResponse

from routstr.core.db import ApiKey, AsyncSession
from routstr.payment.models import Architecture, Model, Pricing, TopProvider
from routstr.upstream.base import BaseUpstreamProvider
from routstr.websearch.types import WebSearchContext

logger = logging.getLogger(__name__)

# =============================================================================
# DATA CLASSES FOR TEST CONFIGURATION
# =============================================================================


@dataclass
class HandlerPath:
    """Defines a handler path to be tested."""

    name: str
    method: str
    is_streaming: bool
    is_x_cashu: bool
    is_responses_api: bool = False
    is_messages: bool = False
    is_litellm: bool = False


@dataclass
class ExpectedWebSearchResult:
    """Expected results from a web search test."""

    web_search_executed: bool
    sources: dict[str, str] | None
    web_search_cost_in_usage: bool
    web_search_msats: int | None


# =============================================================================
# CONSTANTS - ALL TEST PATHS TO COVER
# =============================================================================

ALL_HANDLER_PATHS = [
    # Standard Bearer Auth - Chat Completions
    HandlerPath(
        "handle_streaming_chat_completion",
        "handle_streaming_chat_completion",
        True,
        False,
    ),
    HandlerPath(
        "handle_non_streaming_chat_completion",
        "handle_non_streaming_chat_completion",
        False,
        False,
    ),
    # Standard Bearer Auth - Responses API
    HandlerPath(
        "handle_streaming_responses_completion",
        "handle_streaming_responses_completion",
        True,
        False,
        is_responses_api=True,
    ),
    HandlerPath(
        "handle_non_streaming_responses_completion",
        "handle_non_streaming_responses_completion",
        False,
        False,
        is_responses_api=True,
    ),
    # Standard Bearer Auth - Messages API
    HandlerPath(
        "handle_streaming_messages_completion",
        "handle_streaming_messages_completion",
        True,
        False,
        is_messages=True,
    ),
    HandlerPath(
        "handle_non_streaming_messages_completion",
        "handle_non_streaming_messages_completion",
        False,
        False,
        is_messages=True,
    ),
    # LiteLLM Messages (Bearer Auth)
    HandlerPath(
        "_forward_messages_via_litellm",
        "_forward_messages_via_litellm",
        False,
        False,
        is_messages=True,
        is_litellm=True,
    ),
    HandlerPath(
        "_stream_litellm_messages",
        "_stream_litellm_messages",
        True,
        False,
        is_messages=True,
        is_litellm=True,
    ),
    # X-Cashu - Chat Completions
    HandlerPath(
        "handle_x_cashu_streaming_response",
        "handle_x_cashu_streaming_response",
        True,
        True,
    ),
    HandlerPath(
        "handle_x_cashu_non_streaming_response",
        "handle_x_cashu_non_streaming_response",
        False,
        True,
    ),
    HandlerPath(
        "handle_x_cashu_chat_completion", "handle_x_cashu_chat_completion", False, True
    ),
    # X-Cashu - Responses API
    HandlerPath(
        "handle_x_cashu_streaming_responses_response",
        "handle_x_cashu_streaming_responses_response",
        True,
        True,
        is_responses_api=True,
    ),
    HandlerPath(
        "handle_x_cashu_non_streaming_responses_response",
        "handle_x_cashu_non_streaming_responses_response",
        False,
        True,
        is_responses_api=True,
    ),
    HandlerPath(
        "handle_x_cashu_responses_completion",
        "handle_x_cashu_responses_completion",
        False,
        True,
        is_responses_api=True,
    ),
    # X-Cashu - Messages via LiteLLM
    HandlerPath(
        "_forward_x_cashu_messages_via_litellm",
        "_forward_x_cashu_messages_via_litellm",
        False,
        True,
        is_messages=True,
        is_litellm=True,
    ),
]


# =============================================================================
# FIXTURES (same as before)
# =============================================================================


@pytest.fixture
def model_id() -> str:
    return "gpt-4o"


@pytest.fixture
def mock_model(model_id: str) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=123456789,
        description="Test model",
        context_length=128000,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="cl100k_base",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=0.000005,
            completion=0.000015,
            request=0,
            image=0,
            web_search=0.001,
            internal_reasoning=0,
        ),
        sats_pricing=Pricing(
            prompt=0.5,
            completion=1.5,
            request=0,
            image=0,
            web_search=100,
            internal_reasoning=0,
        ),
        per_request_limits=None,
        top_provider=TopProvider(
            context_length=128000,
            max_completion_tokens=4096,
            is_moderated=False,
        ),
        enabled=True,
        upstream_provider_id=1,
        canonical_slug=model_id,
        alias_ids=[],
    )


@pytest.fixture
def mock_key() -> MagicMock:
    key = MagicMock(spec=ApiKey)
    key.hashed_key = "test-key-hash-12345"
    key.balance = 10000000
    return key


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.refresh = AsyncMock()
    return session


@pytest.fixture
def provider() -> BaseUpstreamProvider:
    return BaseUpstreamProvider(
        base_url="https://api.openai.com",
        api_key="test-api-key",
        provider_fee=1.01,
    )


@pytest.fixture(autouse=True)
def mock_adjust_payment():
    with patch("routstr.upstream.base.adjust_payment_for_tokens") as mock:

        async def side_effect(key, response_data, session, max_cost, web_context=None):
            if web_context and web_context.executed:
                return {
                    "base_msats": 100,
                    "input_msats": 500,
                    "output_msats": 1500,
                    "web_search_msats": 100,
                    "total_msats": 2200,
                    "total_usd": 0.0022,
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            return {
                "base_msats": 100,
                "input_msats": 500,
                "output_msats": 1500,
                "web_search_msats": 0,
                "total_msats": 2100,
                "total_usd": 0.0021,
                "input_tokens": 100,
                "output_tokens": 50,
            }

        mock.side_effect = side_effect
        yield mock


@pytest.fixture(autouse=True)
def mock_create_session(mock_key: MagicMock):
    with patch("routstr.upstream.base.create_session") as mock:
        session = AsyncMock()
        session.get.return_value = mock_key
        mock.return_value.__aenter__ = AsyncMock(return_value=session)
        mock.return_value.__aexit__ = AsyncMock(return_value=None)
        yield mock


# =============================================================================
# HELPER FUNCTIONS FOR BUILDING MOCK RESPONSES
# =============================================================================


def build_openai_chat_response(model: str = "gpt-4o", with_usage: bool = True) -> dict:
    response: dict = {
        "id": "chatcmpl-test-123",
        "object": "chat.completion",
        "created": 1234567890,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Paris is the capital of France.",
                },
                "finish_reason": "stop",
            }
        ],
    }
    if with_usage:
        response["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
    return response


def build_openai_streaming_chunks(
    model: str = "gpt-4o", with_final_usage: bool = True
) -> list[bytes]:
    chunks = [
        b'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1234567890,"model":"'
        + model.encode()
        + b'","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
        b'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1234567890,"model":"'
        + model.encode()
        + b'","choices":[{"index":0,"delta":{"content":"Paris"},"finish_reason":null}]}\n\n',
    ]
    if with_final_usage:
        chunks.append(
            b'data: {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1234567890,"model":"'
            + model.encode()
            + b'","choices":[],"usage":{"prompt_tokens":100,"completion_tokens":50,"total_tokens":150}}\n\n'
        )
    chunks.append(b"data: [DONE]\n\n")
    return chunks


def build_responses_api_response(model: str = "gpt-4o") -> dict:
    return {
        "id": "resp_test_123",
        "object": "response",
        "created_at": 1234567890,
        "model": model,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Paris is the capital of France."}
                ],
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        },
    }


def build_responses_api_streaming_chunks(model: str = "gpt-4o") -> list[bytes]:
    return [
        b'data: {"type":"response.created","response":{"id":"resp_test","model":"'
        + model.encode()
        + b'"}}\n\n',
        b'data: {"type":"response.output_text.delta","delta":"Paris"}\n\n',
        b'data: {"type":"response.completed","response":{"model":"'
        + model.encode()
        + b'","usage":{"input_tokens":100,"output_tokens":50,"total_tokens":150}}}\n\n',
        b"data: [DONE]\n\n",
    ]


def build_anthropic_messages_response(model: str = "claude-3-opus") -> dict:
    return {
        "id": "msg_test_123",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "Paris is the capital of France."}],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
        },
    }


def build_anthropic_messages_streaming_chunks(
    model: str = "claude-3-opus",
) -> list[bytes]:
    return [
        f'event: message_start\ndata: {{"type":"message_start","message":{{"id":"msg_test","model":"{model}","usage":{{"input_tokens":100,"output_tokens":0}}}}}}\n\n'.encode(),
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Paris is the capital of France."}}\n\n',
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":50}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]


# =============================================================================
# UNIFIED ASSERTION HELPERS
# =============================================================================


async def assert_websearch_in_non_streaming_response(
    response: Response,
    expected: ExpectedWebSearchResult,
) -> dict:
    """Assert web search fields are present in a non-streaming response."""
    assert response.status_code == 200
    body = json.loads(response.body)

    # Check web_search_executed flag - only required if expected True
    if expected.web_search_executed:
        assert "web_search_executed" in body, (
            "web_search_executed field missing from response"
        )
        assert body["web_search_executed"] is True
    else:
        # When not expected, field should be either missing or False
        if "web_search_executed" in body:
            assert body["web_search_executed"] is False

    if expected.sources:
        assert "sources" in body, "sources field missing from response"
        assert body["sources"] == expected.sources
    else:
        if "sources" in body:
            assert not body["sources"], (
                "sources should be empty when web search not executed"
            )

    # Check cost structure - X-Cashu uses usage.cost_sats, Bearer uses cost dict
    if "cost" in body:
        cost = body["cost"]
        if expected.web_search_msats is not None:
            assert "web_search_msats" in cost, "web_search_msats missing from cost"
            assert cost["web_search_msats"] == expected.web_search_msats

    # Check usage block has cost info
    if expected.web_search_cost_in_usage and "usage" in body:
        usage = body["usage"]
        assert "cost_sats" in usage, "cost_sats missing from usage"

    return body


async def assert_websearch_in_streaming_response(
    response: StreamingResponse,
    expected: ExpectedWebSearchResult,
) -> list[dict]:
    """Assert web search fields are present in streaming response."""
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    data_chunks: list[dict] = []
    for chunk in chunks:
        chunk_str = chunk.decode("utf-8")
        for line in chunk_str.strip().split("\n"):
            if line.startswith("data: ") and "[DONE]" not in line:
                try:
                    data = json.loads(line[6:])
                    data_chunks.append(data)
                except json.JSONDecodeError:
                    continue

    # Find the final chunk with usage/cost
    if expected.web_search_executed:
        websearch_chunks = [d for d in data_chunks if "web_search_executed" in d]
        assert websearch_chunks, "No chunk with web_search_executed found"
        assert any(d.get("web_search_executed") for d in websearch_chunks)

        sources_chunks = [d for d in data_chunks if "sources" in d]
        assert sources_chunks, "No chunk with sources found"
        if expected.sources:
            assert any(d.get("sources") == expected.sources for d in sources_chunks)

    return data_chunks


# =============================================================================
# TEST RUNNERS
# =============================================================================


async def run_handler_test(
    provider: BaseUpstreamProvider,
    handler_path: HandlerPath,
    mock_key: MagicMock,
    mock_session: AsyncSession,
    web_context: WebSearchContext,
    expected: ExpectedWebSearchResult,
) -> None:
    """Unified test runner for any handler path."""

    handler_method = getattr(provider, handler_path.method)

    if handler_path.is_litellm:
        await _run_litellm_handler(
            handler_method,
            provider,
            handler_path,
            mock_key,
            mock_session,
            web_context,
            expected,
        )
    elif handler_path.is_x_cashu:
        await _run_xcashu_handler(
            handler_method,
            handler_path,
            web_context,
            expected,
        )
    else:
        await _run_bearer_handler(
            handler_method,
            handler_path,
            mock_key,
            mock_session,
            web_context,
            expected,
        )


def _mock_streaming_response(chunks: list[bytes]) -> MagicMock:
    """Create mock streaming response."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {"content-type": "text/event-stream"}

    async def aiter_bytes():
        for chunk in chunks:
            yield chunk

    response.aiter_bytes = aiter_bytes
    response.aread = AsyncMock(return_value=b"")
    return response


def _mock_non_streaming_response(data: dict) -> MagicMock:
    """Create mock non-streaming response."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {"content-type": "application/json"}
    response.aread = AsyncMock(return_value=json.dumps(data).encode())
    return response


async def _run_bearer_handler(
    handler_method: Callable,
    handler_path: HandlerPath,
    mock_key: MagicMock,
    mock_session: AsyncSession,
    web_context: WebSearchContext,
    expected: ExpectedWebSearchResult,
) -> None:
    """Run a Bearer auth handler test"""

    import inspect

    sig = inspect.signature(handler_method)

    # Build appropriate mock response
    if handler_path.is_responses_api:
        if handler_path.is_streaming:
            mock_upstream = _mock_streaming_response(
                build_responses_api_streaming_chunks()
            )
        else:
            mock_upstream = _mock_non_streaming_response(build_responses_api_response())
    elif handler_path.is_messages:
        if handler_path.is_streaming:
            mock_upstream = _mock_streaming_response(
                build_anthropic_messages_streaming_chunks()
            )
        else:
            mock_upstream = _mock_non_streaming_response(
                build_anthropic_messages_response()
            )
    else:
        # Standard chat completions
        if handler_path.is_streaming:
            mock_upstream = _mock_streaming_response(build_openai_streaming_chunks())
        else:
            mock_upstream = _mock_non_streaming_response(build_openai_chat_response())

    kwargs: dict[str, Any] = {
        "response": mock_upstream,
        "key": mock_key,
        "web_context": web_context,
    }

    # Handle different parameter names
    if handler_path.is_streaming:
        # Streaming handlers use max_cost_for_model
        if "max_cost_for_model" in sig.parameters:
            kwargs["max_cost_for_model"] = 5000
        # Some streaming handlers need background_tasks
        if "background_tasks" in sig.parameters:
            kwargs["background_tasks"] = BackgroundTasks()
    else:
        # Non-streaming handlers use deducted_max_cost
        if "deducted_max_cost" in sig.parameters:
            kwargs["deducted_max_cost"] = 5000
        # Or max_cost_for_model for some
        elif "max_cost_for_model" in sig.parameters:
            kwargs["max_cost_for_model"] = 5000

    if "session" in sig.parameters:
        kwargs["session"] = mock_session

    if "path" in sig.parameters:
        kwargs["path"] = "v1/chat/completions"

    result = await handler_method(**kwargs)

    # Assert based on response type
    if isinstance(result, StreamingResponse):
        await assert_websearch_in_streaming_response(result, expected)
    else:
        await assert_websearch_in_non_streaming_response(result, expected)


async def _run_litellm_handler(
    handler_method: Callable,
    provider: BaseUpstreamProvider,
    handler_path: HandlerPath,
    mock_key: MagicMock,
    mock_session: AsyncSession,
    web_context: WebSearchContext,
    expected: ExpectedWebSearchResult,
) -> None:
    """Run a LiteLLM handler test"""

    import inspect

    sig = inspect.signature(handler_method)

    if handler_path.method == "_forward_messages_via_litellm":
        # Non-streaming: takes request_body, returns Response
        request_body = json.dumps(
            {
                "model": "claude-3-opus",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            }
        ).encode()

        kwargs = {
            "request_body": request_body,
            "key": mock_key,
            "max_cost_for_model": 5000,
            "model_obj": MagicMock(),
            "web_context": web_context,
        }

        if "session" in sig.parameters:
            kwargs["session"] = mock_session

        with patch.object(
            provider, "_dispatch_anthropic_messages", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = (
                False,
                build_anthropic_messages_response(),
                "claude-3-opus",
            )
            result = await handler_method(**kwargs)

            await assert_websearch_in_non_streaming_response(result, expected)

    elif handler_path.method == "_stream_litellm_messages":
        # Streaming: takes iterator (AsyncIterator), returns StreamingResponse

        async def mock_iterator():
            """Mock AsyncIterator that yields SSE chunks."""
            for chunk in build_anthropic_messages_streaming_chunks():
                yield chunk

        kwargs = {
            "iterator": mock_iterator(),
            "key": mock_key,
            "max_cost_for_model": 5000,
            "requested_model": "claude-3-opus",
            "web_context": web_context,
        }

        result = handler_method(**kwargs)

        assert isinstance(result, StreamingResponse)
        await assert_websearch_in_streaming_response(result, expected)


async def _run_xcashu_handler(
    handler_method: Callable,
    handler_path: HandlerPath,
    web_context: WebSearchContext,
    expected: ExpectedWebSearchResult,
) -> None:
    """Run an X-Cashu handler test"""

    import inspect

    sig = inspect.signature(handler_method)

    # Build content based on handler type
    if handler_path.is_streaming:
        if handler_path.is_responses_api:
            chunks = build_responses_api_streaming_chunks()
        else:
            chunks = build_openai_streaming_chunks()
        # Convert chunks to a single string (each chunk is already a full SSE line)
        content_str = "".join(chunk.decode("utf-8") for chunk in chunks)
    else:
        # Non-streaming uses raw JSON
        if handler_path.is_responses_api:
            content_data = build_responses_api_response()
        else:
            content_data = build_openai_chat_response()
        content_str = json.dumps(content_data)

    # Mock response
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}

    kwargs: dict[str, Any] = {
        "amount": 10000,
        "unit": "msat",
        "max_cost_for_model": 5000,
        "web_context": web_context,
    }

    # X-Cashu handlers that take content_str directly
    if "content_str" in sig.parameters:
        kwargs["content_str"] = content_str
        kwargs["response"] = mock_response
    elif "response" in sig.parameters:
        # These read from response.aread()
        mock_response.aread = AsyncMock(return_value=content_str.encode())
        kwargs["response"] = mock_response

    if "mint" in sig.parameters:
        kwargs["mint"] = "https://mint.example.com"
    if "payment_token_hash" in sig.parameters:
        kwargs["payment_token_hash"] = "test-hash"
    if "request_id" in sig.parameters:
        kwargs["request_id"] = "test-request-id"

    # Mock send_refund and get_x_cashu_cost
    with patch.object(
        BaseUpstreamProvider,
        "send_refund",
        new_callable=AsyncMock,
        return_value="refund-token",
    ):
        with patch.object(
            BaseUpstreamProvider, "get_x_cashu_cost", new_callable=AsyncMock
        ) as mock_cost:
            mock_cost.return_value = MagicMock(
                total_msats=2200 if expected.web_search_executed else 2100
            )
            result = await handler_method(**kwargs)

    # Assert based on response type
    if isinstance(result, StreamingResponse):
        await assert_websearch_in_streaming_response(result, expected)
    else:
        await assert_websearch_in_non_streaming_response(result, expected)
    assert "X-Cashu" in result.headers or "x-cashu" in result.headers, (
        "X-Cashu refund header missing"
    )


# =============================================================================
# PARAMETERIZED TESTS
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_path", [pytest.param(p, id=p.name) for p in ALL_HANDLER_PATHS]
)
async def test_websearch_executed_all_paths(
    provider: BaseUpstreamProvider,
    mock_key: MagicMock,
    mock_session: AsyncSession,
    handler_path: HandlerPath,
):
    """Test that web search is properly reported across ALL handler paths."""
    web_context = WebSearchContext(
        executed=True,
        sources={
            "1": "https://en.wikipedia.org/wiki/Paris",
            "2": "https://travel.example.com/france",
        },
    )

    expected = ExpectedWebSearchResult(
        web_search_executed=True,
        sources={
            "1": "https://en.wikipedia.org/wiki/Paris",
            "2": "https://travel.example.com/france",
        },
        web_search_cost_in_usage=True,
        web_search_msats=100,
    )

    await run_handler_test(
        provider,
        handler_path,
        mock_key,
        mock_session,
        web_context,
        expected,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_path",
    [
        pytest.param(p, id=p.name)
        for p in ALL_HANDLER_PATHS[:6]  # Just Bearer auth paths
    ],
)
async def test_websearch_not_executed_all_paths(
    provider: BaseUpstreamProvider,
    mock_key: MagicMock,
    mock_session: AsyncSession,
    handler_path: HandlerPath,
):
    """Test that when web search is NOT executed, the flags are not set."""
    web_context = WebSearchContext(executed=False, sources={})

    expected = ExpectedWebSearchResult(
        web_search_executed=False,
        sources=None,
        web_search_cost_in_usage=True,
        web_search_msats=0,
    )

    await run_handler_test(
        provider,
        handler_path,
        mock_key,
        mock_session,
        web_context,
        expected,
    )


# =============================================================================
# FULL FLOW TEST
# =============================================================================


@pytest.mark.asyncio
async def test_full_flow_websearch_injection(
    provider: BaseUpstreamProvider,
    mock_key: MagicMock,
    mock_session: AsyncSession,
    mock_model: Model,
):
    """Integration test verifying web search context through full request flow."""
    from fastapi import Request

    request_body = json.dumps(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "What is the capital of France?"}],
            "stream": False,
            "enable_web_search": True,
        }
    ).encode()

    mock_request = MagicMock(spec=Request)
    mock_request.method = "POST"
    mock_request.headers = {
        "authorization": "Bearer test-key",
        "content-type": "application/json",
    }
    mock_request.body = AsyncMock(return_value=request_body)
    mock_request.query_params = {}

    with patch("routstr.upstream.base.WebManager.is_rag_enabled", return_value=True):
        with patch("routstr.upstream.base.web_manager") as mock_wm:
            mock_wm.enhance_request_with_web_context = AsyncMock(
                return_value={
                    "body": request_body,
                    "websearchcontext": WebSearchContext(
                        executed=True,
                        sources={"1": "https://en.wikipedia.org/wiki/Paris"},
                    ),
                }
            )
            mock_wm.extract_web_search_parameter = lambda b: (b, True)

            # Create mock response
            mock_response = _mock_non_streaming_response(build_openai_chat_response())

            with patch("httpx.AsyncClient") as mock_client_class:
                # Create an async context manager mock that works properly
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                # Mock send as a coroutine that returns the response
                mock_client.send = AsyncMock(return_value=mock_response)
                mock_client.aclose = AsyncMock()

                # The key: __aenter__ must return the mock_client itself
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)

                result = await provider.forward_request(
                    request=mock_request,
                    path="v1/chat/completions",
                    headers={"authorization": "Bearer test-key"},
                    request_body=request_body,
                    key=mock_key,
                    max_cost_for_model=5000,
                    session=mock_session,
                    model_obj=mock_model,
                )

                mock_wm.enhance_request_with_web_context.assert_called_once()

                if hasattr(result, "body"):
                    body = json.loads(result.body)
                    assert body.get("web_search_executed") is True
                    assert "sources" in body
