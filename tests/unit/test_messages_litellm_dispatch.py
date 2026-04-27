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


def test_parse_sse_blocks_extracts_full_events() -> None:
    buffer = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"id":"m1"}}\n\n'
        b"event: message_stop\n"
        b'data: {"type":"message_stop"}\n\n'
    )
    events, remaining = BaseUpstreamProvider._parse_sse_blocks(buffer)
    assert remaining == b""
    assert events == [
        {"type": "message_start", "message": {"id": "m1"}},
        {"type": "message_stop"},
    ]


def test_parse_sse_blocks_preserves_partial_trailing_block() -> None:
    buffer = b'data: {"type":"a"}\n\nevent: b\ndata: {"type":"b"}'
    events, remaining = BaseUpstreamProvider._parse_sse_blocks(buffer)
    assert events == [{"type": "a"}]
    assert remaining == b'event: b\ndata: {"type":"b"}'


def test_parse_sse_blocks_skips_done_and_comments() -> None:
    buffer = b": ping\n\ndata: [DONE]\n\ndata: {\"type\":\"x\"}\n\n"
    events, remaining = BaseUpstreamProvider._parse_sse_blocks(buffer)
    assert remaining == b""
    assert events == [{"type": "x"}]


def test_events_from_chunk_handles_bytes_chunks() -> None:
    provider = _make_provider()
    chunk = (
        b"event: a\ndata: {\"type\":\"a\"}\n\n"
        b"event: b\ndata: {\"type\":\"b\"}"
    )
    events, buf = provider._events_from_chunk(chunk, b"")
    assert events == [{"type": "a"}]
    # second event still partial because no trailing \n\n
    assert buf == b'event: b\ndata: {"type":"b"}'

    # Feed remainder of the second event
    events, buf = provider._events_from_chunk(b"\n\n", buf)
    assert events == [{"type": "b"}]
    assert buf == b""


def test_events_from_chunk_handles_str_chunks() -> None:
    provider = _make_provider()
    events, buf = provider._events_from_chunk(
        'event: a\ndata: {"type":"a"}\n\n', b""
    )
    assert events == [{"type": "a"}]
    assert buf == b""


# ---------------------------------------------------------------------------
# Pydantic serializer warning + usage normalization
# ---------------------------------------------------------------------------


def test_coerce_litellm_payload_silences_pydantic_serializer_warning() -> None:
    """litellm response models emit a benign UserWarning when their nested
    pydantic field (e.g. usage = ResponseAPIUsage) holds a plain dict.
    _coerce_litellm_payload must silence it locally."""
    import warnings as _warnings

    obj = MagicMock()

    def _emit_warning(*args: Any, **kwargs: Any) -> dict:
        _warnings.warn(
            "Pydantic serializer warnings:\n  Expected `ResponseAPIUsage`",
            UserWarning,
            stacklevel=2,
        )
        return {"id": "x", "usage": {"completion_tokens": 5}}

    obj.model_dump.side_effect = _emit_warning

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        out = BaseUpstreamProvider._coerce_litellm_payload(obj)

    assert out == {"id": "x", "usage": {"completion_tokens": 5}}
    serializer_warnings = [
        w for w in caught if "Pydantic serializer warnings" in str(w.message)
    ]
    assert serializer_warnings == [], (
        "Pydantic serializer warning should be suppressed at source"
    )


def test_normalize_litellm_payload_maps_openai_keys_to_anthropic() -> None:
    event = {
        "type": "message_delta",
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 22,
            "total_tokens": 33,
        },
        "message": {
            "model": "x",
            "usage": {"prompt_tokens": 7, "completion_tokens": 13},
        },
    }
    out = BaseUpstreamProvider._normalize_litellm_payload(event)

    # Top-level usage gets canonical Anthropic keys mirrored in
    assert out["usage"]["input_tokens"] == 11
    assert out["usage"]["output_tokens"] == 22
    # Original keys preserved (non-destructive)
    assert out["usage"]["prompt_tokens"] == 11
    assert out["usage"]["completion_tokens"] == 22

    # Nested message.usage normalized too
    assert out["message"]["usage"]["input_tokens"] == 7
    assert out["message"]["usage"]["output_tokens"] == 13

    # Original event must not be mutated
    assert "input_tokens" not in event["usage"]


def test_normalize_litellm_payload_keeps_anthropic_keys_intact() -> None:
    event = {
        "type": "message_delta",
        "usage": {"input_tokens": 4, "output_tokens": 9},
    }
    out = BaseUpstreamProvider._normalize_litellm_payload(event)
    assert out["usage"] == {"input_tokens": 4, "output_tokens": 9}


def test_normalize_litellm_payload_aliases_dumped_usage() -> None:
    """A litellm result whose model_dump emits OpenAI-style usage keys
    should be normalized to Anthropic-style keys for downstream cost
    reconciliation, with the original keys preserved alongside."""
    result = MagicMock()
    result.model_dump.return_value = {
        "id": "abc",
        "model": "openai/gpt-4o-mini",
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
    }

    out = BaseUpstreamProvider._normalize_litellm_payload(result)

    assert out["model"] == "openai/gpt-4o-mini"
    assert out["usage"]["input_tokens"] == 12
    assert out["usage"]["output_tokens"] == 34
    # OpenAI keys preserved alongside the canonical mapping
    assert out["usage"]["prompt_tokens"] == 12


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


@pytest.mark.asyncio
async def test_streaming_handles_iterator_yielding_raw_sse_bytes() -> None:
    """Regression: litellm sometimes yields already-SSE-encoded bytes.

    Previously this raised TypeError("Cannot coerce bytes to dict"). The
    stream loop must parse SSE blocks (even split across chunks) and still
    perform cost reconciliation.
    """
    provider = _make_provider()
    key = _make_key()
    model = _make_model()
    session = _make_session()
    body = _anthropic_request_body(stream=True)

    async def fake_byte_chunks() -> AsyncIterator[bytes]:
        # Whole event in one chunk
        yield (
            b"event: message_start\n"
            b'data: {"type":"message_start","message":{"id":"m1",'
            b'"model":"openai/gpt-4o-mini",'
            b'"usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
        )
        # Event split across two chunks (boundary inside the data line)
        yield b'event: message_delta\ndata: {"type":"message_delta",'
        yield b'"delta":{},"usage":{"output_tokens":4}}\n\n'
        # SSE comment + DONE sentinel must be ignored
        yield b": keepalive\n\ndata: [DONE]\n\n"
        yield b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"

    fake_cost = {"total_msats": 999, "total_usd": 0.0001}
    captured: dict[str, Any] = {}

    async def fake_adjust(
        fresh_key: Any, combined_data: Any, sess: Any, max_cost: int
    ) -> dict:
        captured["combined_data"] = combined_data
        return fake_cost

    fake_session = MagicMock()
    fake_session.get = AsyncMock(return_value=key)

    class FakeSessionCtx:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            return None

    with (
        patch(
            "litellm.anthropic.messages.acreate",
            new=AsyncMock(return_value=fake_byte_chunks()),
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
    assert "event: message_start" in joined
    assert "event: message_delta" in joined
    assert "event: message_stop" in joined
    assert "event: cost" in joined
    # [DONE] sentinel and SSE comments must NOT be re-emitted as data
    assert "[DONE]" not in joined

    combined = captured["combined_data"]
    assert combined["usage"]["input_tokens"] == 3
    assert combined["usage"]["output_tokens"] == 4
    assert combined["model"] == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_streaming_normalizes_openai_usage_keys_for_cost() -> None:
    """Regression: when litellm yields chunks with OpenAI-style usage keys
    (prompt_tokens/completion_tokens), the stream loop must map them to
    Anthropic input_tokens/output_tokens for cost reconciliation."""
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
                # OpenAI-style usage on the message
                "usage": {"prompt_tokens": 9, "completion_tokens": 0},
            },
        }
        yield {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        }
        yield {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            # OpenAI-style usage on the delta
            "usage": {"prompt_tokens": 0, "completion_tokens": 17},
        }
        yield {"type": "message_stop"}

    fake_cost = {"total_msats": 100, "total_usd": 0.0001}
    captured: dict[str, Any] = {}

    async def fake_adjust(
        fresh_key: Any, combined_data: Any, sess: Any, max_cost: int
    ) -> dict:
        captured["combined_data"] = combined_data
        return fake_cost

    fake_session = MagicMock()
    fake_session.get = AsyncMock(return_value=key)

    class FakeSessionCtx:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            return None

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
        async for _ in result.body_iterator:
            pass

    combined = captured["combined_data"]
    # Token counts come from the OpenAI-style fields after normalization
    assert combined["usage"]["input_tokens"] == 9
    assert combined["usage"]["output_tokens"] == 17


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
