"""Unit tests for the litellm-based /v1/messages dispatch path.

Covers `BaseUpstreamProvider._forward_messages_via_litellm` and the
shortcut wired into `forward_request`.
"""

import json
import os
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import Response, StreamingResponse

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.core.db import ApiKey  # noqa: E402
from routstr.payment.models import Architecture, Model, Pricing  # noqa: E402
from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_provider(supports_messages: bool = False) -> BaseUpstreamProvider:
    provider = BaseUpstreamProvider(base_url="http://test", api_key="upstream-key")
    if supports_messages:
        provider.supports_anthropic_messages = True
    return provider


def _make_key() -> ApiKey:
    return ApiKey(hashed_key="abcdef0123" * 4, balance=1_000_000)


def _make_model(model_id: str = "openai/gpt-4o-mini") -> Model:
    return Model(
        id=model_id,
        name=model_id,
        forwarded_model_id=model_id,
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="x",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=0.0,
            completion=0.0,
            request=0.0,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
            max_cost=0.0,
        ),
        sats_pricing=None,
        per_request_limits=None,
        top_provider=None,
    )


def _make_session() -> Any:
    return MagicMock()


def _anthropic_request_body(*, stream: bool = False) -> bytes:
    return json.dumps(
        {
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
            "stream": stream,
        }
    ).encode()


def _anthropic_non_stream_response() -> dict:
    return {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "model": "openai/gpt-4o-mini",
        "content": [{"type": "text", "text": "hello!"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


# ---------------------------------------------------------------------------
# Helper / coercion
# ---------------------------------------------------------------------------


def test_coerce_litellm_payload_handles_dict() -> None:
    out = BaseUpstreamProvider._coerce_litellm_payload({"a": 1})
    assert out == {"a": 1}


def test_coerce_litellm_payload_handles_pydantic_v2() -> None:
    obj = MagicMock()
    obj.model_dump.return_value = {"x": 42}
    # ensure dict() isn't preferred over model_dump
    out = BaseUpstreamProvider._coerce_litellm_payload(obj)
    assert out == {"x": 42}


# ---------------------------------------------------------------------------
# Provider gating
# ---------------------------------------------------------------------------


def test_default_provider_does_not_support_anthropic_messages() -> None:
    assert BaseUpstreamProvider.supports_anthropic_messages is False
    assert BaseUpstreamProvider.litellm_provider_prefix == "openai/"


def test_anthropic_provider_supports_native_messages() -> None:
    from routstr.upstream.anthropic import AnthropicUpstreamProvider

    assert AnthropicUpstreamProvider.supports_anthropic_messages is True


def test_openrouter_provider_supports_native_messages() -> None:
    from routstr.upstream.openrouter import OpenRouterUpstreamProvider

    assert OpenRouterUpstreamProvider.supports_anthropic_messages is True


def test_provider_prefix_overrides() -> None:
    from routstr.upstream.azure import AzureUpstreamProvider
    from routstr.upstream.fireworks import FireworksUpstreamProvider
    from routstr.upstream.gemini import GeminiUpstreamProvider
    from routstr.upstream.groq import GroqUpstreamProvider
    from routstr.upstream.ollama import OllamaUpstreamProvider
    from routstr.upstream.perplexity import PerplexityUpstreamProvider
    from routstr.upstream.xai import XAIUpstreamProvider

    assert GroqUpstreamProvider.litellm_provider_prefix == "groq/"
    assert XAIUpstreamProvider.litellm_provider_prefix == "xai/"
    assert FireworksUpstreamProvider.litellm_provider_prefix == "fireworks_ai/"
    assert PerplexityUpstreamProvider.litellm_provider_prefix == "perplexity/"
    assert GeminiUpstreamProvider.litellm_provider_prefix == "gemini/"
    assert OllamaUpstreamProvider.litellm_provider_prefix == "ollama_chat/"
    assert AzureUpstreamProvider.litellm_provider_prefix == "azure/"


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streaming_dispatches_via_litellm_and_returns_anthropic_response() -> (
    None
):
    provider = _make_provider()
    key = _make_key()
    model = _make_model()
    session = _make_session()
    body = _anthropic_request_body(stream=False)

    upstream_response = _anthropic_non_stream_response()

    async def fake_acreate(**kwargs: Any) -> dict:
        # Verify model prefixing and credentials are forwarded correctly
        assert kwargs["model"] == "openai/openai/gpt-4o-mini"
        assert kwargs["api_base"] == "http://test"
        assert kwargs["api_key"] == "upstream-key"
        assert kwargs["stream"] is False
        assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
        assert kwargs["max_tokens"] == 64
        return upstream_response

    fake_cost = {"total_msats": 1234, "total_usd": 0.0001}

    with (
        patch(
            "litellm.anthropic.messages.acreate",
            new=AsyncMock(side_effect=fake_acreate),
        ),
        patch(
            "routstr.upstream.base.adjust_payment_for_tokens",
            new=AsyncMock(return_value=fake_cost),
        ),
    ):
        result = await provider._forward_messages_via_litellm(
            request_body=body,
            key=key,
            session=session,
            max_cost_for_model=10_000,
            model_obj=model,
        )

    assert isinstance(result, Response)
    payload = json.loads(result.body)
    assert payload["model"] == "openai/gpt-4o-mini"  # mapped back to requested
    assert payload["usage"]["input_tokens"] == 5
    assert payload["usage"]["output_tokens"] == 3
    # Cost metadata injected
    assert payload["usage"]["cost"] == 0.0001
    assert payload["usage"]["cost_sats"] == 1


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_emits_sse_and_reconciles_cost_at_end() -> None:
    provider = _make_provider()
    key = _make_key()
    model = _make_model()
    session = _make_session()
    body = _anthropic_request_body(stream=True)

    async def fake_chunks() -> AsyncIterator[dict]:
        yield {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "openai/gpt-4o-mini",
                "content": [],
                "usage": {"input_tokens": 5, "output_tokens": 0},
            },
        }
        yield {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
        yield {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        }
        yield {"type": "content_block_stop", "index": 0}
        yield {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 7},
        }
        yield {"type": "message_stop"}

    fake_cost = {"total_msats": 4321, "total_usd": 0.00015}

    captured_cost_call: dict[str, Any] = {}

    async def fake_adjust(
        fresh_key: Any, combined_data: Any, sess: Any, max_cost: int
    ) -> dict:
        captured_cost_call["combined_data"] = combined_data
        captured_cost_call["max_cost"] = max_cost
        return fake_cost

    fake_session = MagicMock()

    class FakeSessionCtx:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            return None

    fake_session.get = AsyncMock(return_value=key)

    with (
        patch(
            "litellm.anthropic.messages.acreate",
            new=AsyncMock(return_value=fake_chunks()),
        ),
        patch(
            "routstr.upstream.base.adjust_payment_for_tokens",
            new=AsyncMock(side_effect=fake_adjust),
        ),
        patch(
            "routstr.upstream.base.create_session",
            new=lambda: FakeSessionCtx(),
        ),
    ):
        result = await provider._forward_messages_via_litellm(
            request_body=body,
            key=key,
            session=session,
            max_cost_for_model=10_000,
            model_obj=model,
        )

        assert isinstance(result, StreamingResponse)
        emitted: list[bytes] = []
        async for chunk in result.body_iterator:
            if isinstance(chunk, bytes):
                emitted.append(chunk)
            elif isinstance(chunk, memoryview):
                emitted.append(bytes(chunk))
            else:
                emitted.append(chunk.encode())

    joined = b"".join(emitted).decode()
    # SSE event header for the first chunk
    assert "event: message_start" in joined
    assert "event: content_block_delta" in joined
    assert "event: message_delta" in joined
    assert "event: cost" in joined

    # Usage was extracted from message_start (input) and message_delta (output)
    combined = captured_cost_call["combined_data"]
    assert combined["usage"]["input_tokens"] == 5
    assert combined["usage"]["output_tokens"] == 7
    assert combined["model"] == "openai/gpt-4o-mini"


# ---------------------------------------------------------------------------
# forward_request gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_request_routes_messages_via_litellm() -> None:
    provider = _make_provider()
    key = _make_key()
    model = _make_model()
    session = _make_session()
    request = MagicMock()

    sentinel = Response(content=b'{"ok": true}', media_type="application/json")
    with patch.object(
        provider,
        "_forward_messages_via_litellm",
        new=AsyncMock(return_value=sentinel),
    ) as mock_helper:
        result = await provider.forward_request(
            request=request,
            path="messages",
            headers={},
            request_body=_anthropic_request_body(),
            key=key,
            max_cost_for_model=10_000,
            session=session,
            model_obj=model,
        )

    mock_helper.assert_awaited_once()
    assert result is sentinel


@pytest.mark.asyncio
async def test_forward_request_skips_litellm_when_provider_supports_messages() -> None:
    provider = _make_provider(supports_messages=True)
    key = _make_key()
    model = _make_model()
    session = _make_session()
    request = MagicMock()

    with patch.object(
        provider,
        "_forward_messages_via_litellm",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        # The non-litellm path will try to build a URL and call httpx, so we
        # short-circuit by patching prepare_request_body to raise — we only
        # care that the litellm helper was NOT invoked.
        with patch.object(
            provider,
            "prepare_request_body",
            side_effect=RuntimeError("stop here"),
        ):
            with pytest.raises(RuntimeError, match="stop here"):
                await provider.forward_request(
                    request=request,
                    path="messages",
                    headers={},
                    request_body=_anthropic_request_body(),
                    key=key,
                    max_cost_for_model=10_000,
                    session=session,
                    model_obj=model,
                )


@pytest.mark.asyncio
async def test_forward_request_skips_litellm_for_count_tokens() -> None:
    provider = _make_provider()
    key = _make_key()
    model = _make_model()
    session = _make_session()
    request = MagicMock()

    with patch.object(
        provider,
        "_forward_messages_via_litellm",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        with patch.object(
            provider,
            "prepare_request_body",
            side_effect=RuntimeError("stop here"),
        ):
            with pytest.raises(RuntimeError, match="stop here"):
                await provider.forward_request(
                    request=request,
                    path="messages/count_tokens",
                    headers={},
                    request_body=_anthropic_request_body(),
                    key=key,
                    max_cost_for_model=10_000,
                    session=session,
                    model_obj=model,
                )
