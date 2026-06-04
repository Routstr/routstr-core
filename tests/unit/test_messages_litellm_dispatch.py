"""Unit tests for the litellm-based /v1/messages dispatch path.

Covers `BaseUpstreamProvider._forward_messages_via_litellm` (bearer-key),
`BaseUpstreamProvider._forward_x_cashu_messages_via_litellm` (x-cashu),
and the shortcuts wired into `forward_request` and
`forward_x_cashu_request`.
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
from routstr.payment.cost_calculation import CostData  # noqa: E402
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


def _make_model(
    model_id: str = "openai/gpt-4o-mini",
    forwarded_model_id: str | None = None,
) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        forwarded_model_id=forwarded_model_id if forwarded_model_id is not None else model_id,
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


def _make_request(request_id: str | None = "req-test") -> Any:
    request = MagicMock()
    request.state.request_id = request_id
    return request


# ---------------------------------------------------------------------------
# Helper / coercion
# ---------------------------------------------------------------------------


def test_coerce_litellm_payload_handles_dict() -> None:
    out = BaseUpstreamProvider._coerce_litellm_payload({"a": 1})
    assert out == {"a": 1}


def test_coerce_litellm_payload_handles_pydantic_v2() -> None:
    obj = MagicMock()
    obj.model_dump.return_value = {"x": 42}
    out = BaseUpstreamProvider._coerce_litellm_payload(obj)
    assert out == {"x": 42}


def test_coerce_litellm_payload_rejects_unknown_types() -> None:
    with pytest.raises(TypeError):
        BaseUpstreamProvider._coerce_litellm_payload(object())


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
    buffer = b': ping\n\ndata: [DONE]\n\ndata: {"type":"x"}\n\n'
    events, remaining = BaseUpstreamProvider._parse_sse_blocks(buffer)
    assert remaining == b""
    assert events == [{"type": "x"}]


def test_events_from_chunk_handles_bytes_chunks() -> None:
    provider = _make_provider()
    chunk = b'event: a\ndata: {"type":"a"}\n\nevent: b\ndata: {"type":"b"}'
    events, buf = provider._events_from_chunk(chunk, b"")
    assert events == [{"type": "a"}]
    assert buf == b'event: b\ndata: {"type":"b"}'

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


def test_compute_refund_msat() -> None:
    assert BaseUpstreamProvider._compute_refund(10_000, "msat", 4_000) == 6_000


def test_compute_refund_sat_rounds_up_cost() -> None:
    # 4001 msats → 5 sats (ceiling). 10 sats - 5 = 5 sats refund.
    assert BaseUpstreamProvider._compute_refund(10, "sat", 4001) == 5


def test_compute_refund_invalid_unit() -> None:
    with pytest.raises(ValueError):
        BaseUpstreamProvider._compute_refund(10, "btc", 100)


# ---------------------------------------------------------------------------
# Provider gating
# ---------------------------------------------------------------------------


def test_default_provider_does_not_support_anthropic_messages() -> None:
    assert BaseUpstreamProvider.supports_anthropic_messages is False
    # Class default is None so URL detection runs at dispatch time.
    assert BaseUpstreamProvider.litellm_provider_prefix is None


def test_base_provider_resolves_prefix_from_base_url() -> None:
    """The bug case: a custom row pointing at Fireworks must resolve to
    `fireworks_ai/` instead of falling back to `openai/`."""
    fireworks = BaseUpstreamProvider(
        base_url="https://api.fireworks.ai/inference/v1", api_key="sk-test"
    )
    assert fireworks.get_litellm_provider_prefix() == "fireworks_ai/"

    groq = BaseUpstreamProvider(
        base_url="https://api.groq.com/openai/v1", api_key="sk-test"
    )
    assert groq.get_litellm_provider_prefix() == "groq/"

    xai = BaseUpstreamProvider(
        base_url="https://api.x.ai/v1", api_key="sk-test"
    )
    assert xai.get_litellm_provider_prefix() == "xai/"

    deepseek = BaseUpstreamProvider(
        base_url="https://api.deepseek.com/v1", api_key="sk-test"
    )
    assert deepseek.get_litellm_provider_prefix() == "deepseek/"

    unknown = BaseUpstreamProvider(
        base_url="https://example.com/v1", api_key="sk-test"
    )
    assert unknown.get_litellm_provider_prefix() == "openai/"


def test_subclass_prefix_wins_over_url_detection() -> None:
    """A subclass override must beat URL detection. e.g. configuring an
    AnthropicUpstreamProvider with a fireworks URL still produces
    ``anthropic/`` (defensive: subclasses pin their backend on purpose)."""
    from routstr.upstream.anthropic import AnthropicUpstreamProvider

    p = AnthropicUpstreamProvider(api_key="sk-test")
    p.base_url = "https://api.fireworks.ai/inference/v1"
    assert p.get_litellm_provider_prefix() == "anthropic/"


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
# Bearer-key non-streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_strips_anthropic_only_fields_before_litellm() -> None:
    """Regression: Anthropic-Messages-only fields like `output_config`,
    `thinking`, `context_management`, and `cache_control` must be removed
    from the request body before litellm dispatches to non-Anthropic
    upstreams. Otherwise upstream returns 400 (unknown field).
    """
    provider = _make_provider()
    key = _make_key()
    model = _make_model()
    session = _make_session()

    body = json.dumps(
        {
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
            "thinking": {"type": "adaptive"},
            "context_management": {"edits": []},
            "output_config": {"effort": "medium"},
            "cache_control": {"type": "ephemeral"},
            "mcp_servers": [],
            "service_tier": "auto",
            "anthropic_beta": "abc",
            "anthropic_version": "2023-06-01",
        }
    ).encode()

    captured: dict[str, Any] = {}

    async def fake_acreate(**kwargs: Any) -> dict:
        captured["kwargs"] = kwargs
        return _anthropic_non_stream_response()

    with (
        patch(
            "litellm.anthropic.messages.acreate",
            new=AsyncMock(side_effect=fake_acreate),
        ),
        patch(
            "routstr.upstream.base.adjust_payment_for_tokens",
            new=AsyncMock(return_value={"total_msats": 0, "total_usd": 0.0}),
        ),
    ):
        await provider._forward_messages_via_litellm(
            request_body=body,
            key=key,
            session=session,
            max_cost_for_model=10_000,
            model_obj=model,
        )

    forwarded = captured["kwargs"]
    for stripped in (
        "thinking",
        "context_management",
        "output_config",
        "cache_control",
        "mcp_servers",
        "service_tier",
        "anthropic_beta",
        "anthropic_version",
    ):
        assert stripped not in forwarded, (
            f"Anthropic-only field {stripped!r} leaked through to litellm"
        )
    # Core fields preserved
    assert forwarded["max_tokens"] == 64
    assert forwarded["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_dispatch_uses_model_id_for_upstream_and_forwarded_for_client() -> None:
    """Convention: `model.id` is the canonical upstream model name;
    `forwarded_model_id` is the public alias echoed back to the client.
    """
    provider = _make_provider()
    key = _make_key()
    # Upstream knows "gpt-4o-mini"; clients see public alias "gpt-5.4-test".
    model = _make_model(
        model_id="gpt-4o-mini",
        forwarded_model_id="gpt-5.4-test",
    )
    session = _make_session()

    captured: dict[str, Any] = {}

    async def fake_acreate(**kwargs: Any) -> dict:
        captured["model"] = kwargs["model"]
        resp = _anthropic_non_stream_response()
        resp["model"] = "gpt-4o-mini"  # upstream echoes its own id
        return resp

    with (
        patch(
            "litellm.anthropic.messages.acreate",
            new=AsyncMock(side_effect=fake_acreate),
        ),
        patch(
            "routstr.upstream.base.adjust_payment_for_tokens",
            new=AsyncMock(return_value={"total_msats": 0, "total_usd": 0.0}),
        ),
    ):
        result = await provider._forward_messages_via_litellm(
            request_body=_anthropic_request_body(stream=False),
            key=key,
            session=session,
            max_cost_for_model=10_000,
            model_obj=model,
        )

    # Upstream call uses `model.id` with provider prefix
    assert captured["model"] == "openai/gpt-4o-mini"
    # Response to client echoes the public `forwarded_model_id`
    assert isinstance(result, Response)
    body = json.loads(result.body)
    assert body["model"] == "gpt-5.4-test"


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
        assert kwargs["model"] == "openai/openai/gpt-4o-mini"
        assert kwargs["api_base"] == "http://test"
        assert kwargs["api_key"] == "upstream-key"
        # The dispatcher always streams from upstream and aggregates back
        # when the client wants a non-streaming response (sidesteps
        # provider-specific non-streaming caps like Fireworks's max_tokens
        # > 4096). When the mock returns a plain dict, the dispatcher
        # detects the lack of __aiter__ and skips aggregation, so the
        # client still gets the same Response shape.
        assert kwargs["stream"] is True
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
    assert payload["usage"]["cost"] == 0.0001
    assert payload["usage"]["cost_sats"] == 1


# ---------------------------------------------------------------------------
# Bearer-key streaming
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
    assert "event: message_start" in joined
    assert "event: content_block_delta" in joined
    assert "event: message_delta" in joined
    assert "event: cost" in joined

    combined = captured_cost_call["combined_data"]
    assert combined["usage"]["input_tokens"] == 5
    assert combined["usage"]["output_tokens"] == 7
    assert combined["model"] == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_streaming_handles_iterator_yielding_raw_sse_bytes() -> None:
    """Regression: litellm.anthropic.messages.acreate(stream=True) yields
    already-SSE-encoded bytes in production. The stream loop must parse
    SSE blocks (even split across chunks) and still reconcile cost.
    """
    provider = _make_provider()
    key = _make_key()
    model = _make_model()
    session = _make_session()
    body = _anthropic_request_body(stream=True)

    async def fake_byte_chunks() -> AsyncIterator[bytes]:
        yield (
            b"event: message_start\n"
            b'data: {"type":"message_start","message":{"id":"m1",'
            b'"model":"openai/gpt-4o-mini",'
            b'"usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
        )
        yield b'event: message_delta\ndata: {"type":"message_delta",'
        yield b'"delta":{},"usage":{"output_tokens":4}}\n\n'
        yield b": keepalive\n\ndata: [DONE]\n\n"
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

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
    assert "[DONE]" not in joined

    combined = captured["combined_data"]
    assert combined["usage"]["input_tokens"] == 3
    assert combined["usage"]["output_tokens"] == 4
    assert combined["model"] == "openai/gpt-4o-mini"


# ---------------------------------------------------------------------------
# x-cashu non-streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_x_cashu_non_streaming_dispatches_and_refunds_overpaid_amount() -> None:
    provider = _make_provider()
    model = _make_model()
    body = _anthropic_request_body(stream=False)
    upstream_response = _anthropic_non_stream_response()

    cost = CostData(
        base_msats=0,
        input_msats=1_000_000,
        output_msats=2_000_000,
        total_msats=3_000_000,
        input_tokens=5,
        output_tokens=3,
    )

    with (
        patch(
            "litellm.anthropic.messages.acreate",
            new=AsyncMock(return_value=upstream_response),
        ),
        patch.object(
            provider,
            "get_x_cashu_cost",
            new=AsyncMock(return_value=cost),
        ),
        patch.object(
            provider,
            "send_refund",
            new=AsyncMock(return_value="cashuREFUND"),
        ) as mock_refund,
    ):
        result = await provider._forward_x_cashu_messages_via_litellm(
            request_body=body,
            amount=10_000,  # sats
            unit="sat",
            max_cost_for_model=10_000,
            model_obj=model,
            mint="https://mint.example",
            request_id="req-1",
        )

    assert isinstance(result, Response)
    assert result.headers.get("X-Cashu") == "cashuREFUND"
    payload = json.loads(result.body)
    # 3_000_000 msats → 3000 sats. Refund = 10_000 - 3000 = 7000.
    mock_refund.assert_awaited_once()
    refund_call = mock_refund.await_args
    assert refund_call is not None
    assert refund_call.args[0] == 7_000
    assert refund_call.args[1] == "sat"
    # cost_sats injected into usage
    assert payload["usage"]["cost_sats"] == 3_000


@pytest.mark.asyncio
async def test_x_cashu_non_streaming_no_refund_when_fully_consumed() -> None:
    provider = _make_provider()
    model = _make_model()
    body = _anthropic_request_body(stream=False)
    upstream_response = _anthropic_non_stream_response()

    cost = CostData(
        base_msats=0,
        input_msats=10_000_000,
        output_msats=0,
        total_msats=10_000_000,
        input_tokens=5,
        output_tokens=3,
    )

    with (
        patch(
            "litellm.anthropic.messages.acreate",
            new=AsyncMock(return_value=upstream_response),
        ),
        patch.object(
            provider,
            "get_x_cashu_cost",
            new=AsyncMock(return_value=cost),
        ),
        patch.object(
            provider,
            "send_refund",
            new=AsyncMock(return_value="cashuNOPE"),
        ) as mock_refund,
    ):
        result = await provider._forward_x_cashu_messages_via_litellm(
            request_body=body,
            amount=10_000,
            unit="sat",
            max_cost_for_model=10_000,
            model_obj=model,
        )

    assert isinstance(result, Response)
    assert "X-Cashu" not in result.headers
    mock_refund.assert_not_awaited()


# ---------------------------------------------------------------------------
# x-cashu streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_x_cashu_streaming_replays_events_and_sets_refund_header() -> None:
    provider = _make_provider()
    model = _make_model()
    body = _anthropic_request_body(stream=True)

    async def fake_chunks() -> AsyncIterator[dict]:
        yield {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "openai/gpt-4o-mini",
                "usage": {"input_tokens": 5, "output_tokens": 0},
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
            "usage": {"output_tokens": 7},
        }
        yield {"type": "message_stop"}

    cost = CostData(
        base_msats=0,
        input_msats=1_000_000,
        output_msats=500_000,
        total_msats=1_500_000,
        input_tokens=5,
        output_tokens=7,
    )

    with (
        patch(
            "litellm.anthropic.messages.acreate",
            new=AsyncMock(return_value=fake_chunks()),
        ),
        patch.object(
            provider,
            "get_x_cashu_cost",
            new=AsyncMock(return_value=cost),
        ),
        patch.object(
            provider,
            "send_refund",
            new=AsyncMock(return_value="cashuSTREAM"),
        ) as mock_refund,
    ):
        result = await provider._forward_x_cashu_messages_via_litellm(
            request_body=body,
            amount=5_000,
            unit="sat",
            max_cost_for_model=10_000,
            model_obj=model,
        )

    assert isinstance(result, StreamingResponse)
    assert result.headers.get("X-Cashu") == "cashuSTREAM"
    # 1_500_000 msats → 1500 sats. Refund = 5000 - 1500 = 3500.
    mock_refund.assert_awaited_once()
    refund_call = mock_refund.await_args
    assert refund_call is not None
    assert refund_call.args[0] == 3_500

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


# ---------------------------------------------------------------------------
# forward_request gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_request_routes_messages_via_litellm() -> None:
    provider = _make_provider()
    key = _make_key()
    model = _make_model()
    session = _make_session()
    request = _make_request()

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
    request = _make_request()

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
                    path="messages",
                    headers={},
                    request_body=_anthropic_request_body(),
                    key=key,
                    max_cost_for_model=10_000,
                    session=session,
                    model_obj=model,
                )


@pytest.mark.asyncio
async def test_forward_request_handles_count_tokens_locally() -> None:
    provider = _make_provider()
    key = _make_key()
    model = _make_model()
    session = _make_session()
    request = _make_request()

    with patch.object(
        provider,
        "_forward_messages_via_litellm",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        with patch.object(
            provider,
            "prepare_request_body",
            side_effect=AssertionError("upstream should not be called"),
        ):
            response = await provider.forward_request(
                request=request,
                path="messages/count_tokens",
                headers={},
                request_body=_anthropic_request_body(),
                key=key,
                max_cost_for_model=10_000,
                session=session,
                model_obj=model,
            )

    assert response.status_code == 200
    body = response.body if isinstance(response.body, bytes) else bytes(response.body)
    payload = json.loads(body.decode())
    assert "input_tokens" in payload
    assert isinstance(payload["input_tokens"], int)
    assert payload["input_tokens"] >= 0


# ---------------------------------------------------------------------------
# forward_x_cashu_request gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_x_cashu_request_routes_messages_via_litellm() -> None:
    provider = _make_provider()
    model = _make_model()
    request = _make_request()
    request.body = AsyncMock(return_value=_anthropic_request_body())

    sentinel = Response(content=b'{"ok": true}', media_type="application/json")

    with patch.object(
        provider,
        "_forward_x_cashu_messages_via_litellm",
        new=AsyncMock(return_value=sentinel),
    ) as mock_helper:
        result = await provider.forward_x_cashu_request(
            request=request,
            path="v1/messages",
            headers={},
            amount=5_000,
            unit="sat",
            max_cost_for_model=10_000,
            model_obj=model,
            mint="https://mint",
        )

    mock_helper.assert_awaited_once()
    helper_call = mock_helper.await_args
    assert helper_call is not None
    kwargs = helper_call.kwargs
    assert kwargs["amount"] == 5_000
    assert kwargs["unit"] == "sat"
    assert kwargs["mint"] == "https://mint"
    assert kwargs["request_id"] == "req-test"
    assert result is sentinel


@pytest.mark.asyncio
async def test_forward_x_cashu_request_skips_litellm_when_native_messages() -> None:
    provider = _make_provider(supports_messages=True)
    model = _make_model()
    request = _make_request()
    request.body = AsyncMock(return_value=_anthropic_request_body())

    with patch.object(
        provider,
        "_forward_x_cashu_messages_via_litellm",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        with patch.object(
            provider,
            "prepare_request_body",
            side_effect=RuntimeError("stop here"),
        ):
            with pytest.raises(RuntimeError, match="stop here"):
                await provider.forward_x_cashu_request(
                    request=request,
                    path="v1/messages",
                    headers={},
                    amount=5_000,
                    unit="sat",
                    max_cost_for_model=10_000,
                    model_obj=model,
                )


@pytest.mark.asyncio
async def test_forward_x_cashu_request_handles_count_tokens_locally() -> None:
    provider = _make_provider()
    model = _make_model()
    request = _make_request()
    request.body = AsyncMock(return_value=_anthropic_request_body())

    with patch.object(
        provider,
        "_forward_x_cashu_messages_via_litellm",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        with patch.object(
            provider,
            "prepare_request_body",
            side_effect=AssertionError("upstream should not be called"),
        ):
            response = await provider.forward_x_cashu_request(
                request=request,
                path="v1/messages/count_tokens",
                headers={},
                amount=5_000,
                unit="sat",
                max_cost_for_model=10_000,
                model_obj=model,
                mint="https://mint",
            )

    assert response.status_code == 200
    body = response.body if isinstance(response.body, bytes) else bytes(response.body)
    payload = json.loads(body.decode())
    assert "input_tokens" in payload


# ---------------------------------------------------------------------------
# Upstream-always-streams + aggregate-on-non-streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregator_assembles_text_message_from_events() -> None:
    """When the client wants a non-streaming response, the dispatcher
    drains the upstream stream and assembles a single Anthropic Message
    dict. This is what makes Fireworks (which rejects max_tokens > 4096
    unless stream=true) work transparently for non-streaming clients."""
    provider = _make_provider()

    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_aggregated",
                "type": "message",
                "role": "assistant",
                "model": "accounts/fireworks/models/glm-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": ", world!"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 42},
        },
        {"type": "message_stop"},
    ]

    async def event_iter() -> AsyncIterator[dict]:
        for event in events:
            yield event

    message = await provider._aggregate_anthropic_events_to_message(event_iter())

    assert message["id"] == "msg_aggregated"
    assert message["role"] == "assistant"
    assert message["stop_reason"] == "end_turn"
    assert message["stop_sequence"] is None
    assert message["content"] == [{"type": "text", "text": "Hello, world!"}]
    assert message["usage"]["input_tokens"] == 10
    assert message["usage"]["output_tokens"] == 42


@pytest.mark.asyncio
async def test_aggregator_parses_tool_use_input_json_delta() -> None:
    """tool_use blocks split their `input` across multiple
    `input_json_delta` chunks. The aggregator must concatenate and parse
    them into a single JSON object."""
    provider = _make_provider()

    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_tool",
                "type": "message",
                "role": "assistant",
                "model": "test-model",
                "content": [],
                "usage": {"input_tokens": 5, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "calc",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"x":'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": ' 7}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]

    async def event_iter() -> AsyncIterator[dict]:
        for event in events:
            yield event

    message = await provider._aggregate_anthropic_events_to_message(event_iter())
    assert message["stop_reason"] == "tool_use"
    assert message["content"][0]["type"] == "tool_use"
    assert message["content"][0]["input"] == {"x": 7}


@pytest.mark.asyncio
async def test_aggregator_parses_sse_byte_chunks() -> None:
    """litellm's anthropic adapter often yields raw SSE byte chunks. The
    aggregator must parse the SSE wire format, not just typed dicts."""
    provider = _make_provider()

    sse = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"m1","type":"message",'
        b'"role":"assistant","model":"x","content":[],"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n\n'
    )

    async def chunk_iter() -> AsyncIterator[bytes]:
        # Split mid-event to exercise the partial-buffer path.
        yield sse[:100]
        yield sse[100:]

    message = await provider._aggregate_anthropic_events_to_message(chunk_iter())
    assert message["content"] == [{"type": "text", "text": "hi"}]
    assert message["stop_reason"] == "end_turn"
    assert message["usage"]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_dispatch_always_streams_upstream_and_aggregates_for_non_streaming_client(  # noqa: E501
) -> None:
    """End-to-end: client says stream=false; dispatcher upstream-streams
    and returns an aggregated Anthropic Message dict."""
    provider = _make_provider()
    model = _make_model()
    body = _anthropic_request_body(stream=False)

    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_e2e",
                "type": "message",
                "role": "assistant",
                "model": "openai/gpt-4o-mini",
                "content": [],
                "usage": {"input_tokens": 4, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "ok"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]

    async def event_iter() -> AsyncIterator[dict]:
        for event in events:
            yield event

    captured_kwargs: dict[str, Any] = {}

    async def fake_acreate(**kwargs: Any) -> AsyncIterator[dict]:
        captured_kwargs.update(kwargs)
        return event_iter()

    with patch(
        "litellm.anthropic.messages.acreate",
        new=AsyncMock(side_effect=fake_acreate),
    ):
        client_stream, result, requested_model = (
            await provider._dispatch_anthropic_messages(
                request_body=body,
                model_obj=model,
            )
        )

    # Upstream was streamed regardless of client preference.
    assert captured_kwargs["stream"] is True
    # Client wanted non-streaming → aggregator ran.
    assert client_stream is False
    assert isinstance(result, dict)
    assert result["content"] == [{"type": "text", "text": "ok"}]
    assert result["stop_reason"] == "end_turn"
    assert requested_model == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_dispatch_uses_url_detected_prefix_for_fireworks_custom_row() -> None:
    """The original bug: a custom-typed row pointing at Fireworks must
    dispatch with `fireworks_ai/`, not `openai/`."""
    provider = BaseUpstreamProvider(
        base_url="https://api.fireworks.ai/inference/v1",
        api_key="fw-key",
    )
    model = _make_model(model_id="accounts/fireworks/models/glm-5")
    body = _anthropic_request_body(stream=True)

    captured_kwargs: dict[str, Any] = {}

    async def empty_iter() -> AsyncIterator[dict]:
        if False:
            yield {}

    async def fake_acreate(**kwargs: Any) -> AsyncIterator[dict]:
        captured_kwargs.update(kwargs)
        return empty_iter()

    with patch(
        "litellm.anthropic.messages.acreate",
        new=AsyncMock(side_effect=fake_acreate),
    ):
        await provider._dispatch_anthropic_messages(
            request_body=body, model_obj=model
        )

    assert captured_kwargs["model"] == (
        "fireworks_ai/accounts/fireworks/models/glm-5"
    )
    assert captured_kwargs["api_base"] == "https://api.fireworks.ai/inference/v1"
