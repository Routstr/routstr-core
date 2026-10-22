"""Unit tests for the Gemini /v1/messages dispatch path.

The Gemini upstream needs special handling because its OpenAI-compat
surface rejects inbound ``functionCall`` parts that lack a
``thought_signature``. The dispatcher (``routstr/upstream/gemini_messages.py``)
injects Google's documented dummy signature
(``"skip_thought_signature_validator"``) on every replayed tool call.

These tests cover ``inject_thought_signatures`` and the dispatcher end to end
against a mocked Gemini compat endpoint.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

import routstr.upstream.gemini_messages as gemini_messages
from routstr.core.exceptions import UpstreamError
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.gemini_messages import (
    DUMMY_THOUGHT_SIGNATURE,
    inject_thought_signatures,
)
from routstr.upstream.messages_dispatch import events_from_chunk
from routstr.upstream.messages_transport import ResponseOwnedIterator

# ---------------------------------------------------------------------------
# inject_thought_signatures
# ---------------------------------------------------------------------------


def test_inject_thought_signatures_adds_dummy_to_each_tool_call() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "do thing"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": "{}"},
                },
                {
                    "id": "toolu_2",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                },
            ],
        },
    ]

    inject_thought_signatures(messages)

    for tc in messages[1]["tool_calls"]:
        assert (
            tc["extra_content"]["google"]["thought_signature"]
            == DUMMY_THOUGHT_SIGNATURE
        )


def test_inject_thought_signatures_preserves_existing_signature() -> None:
    """Don't clobber a real signature that came back from a prior turn."""
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "tc1",
                    "type": "function",
                    "function": {"name": "fn", "arguments": "{}"},
                    "extra_content": {
                        "google": {"thought_signature": "real-signature"}
                    },
                }
            ],
        }
    ]

    inject_thought_signatures(messages)

    assert (
        messages[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"]
        == "real-signature"
    )


def test_inject_thought_signatures_skips_messages_without_tool_calls() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]

    inject_thought_signatures(messages)

    for m in messages:
        assert "extra_content" not in m


def test_inject_thought_signatures_handles_malformed_extra_content() -> None:
    """If a caller already set ``extra_content`` to a non-dict (defensive),
    we replace it instead of crashing."""
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "tc",
                    "type": "function",
                    "function": {"name": "fn", "arguments": "{}"},
                    "extra_content": "garbage",
                }
            ],
        }
    ]

    inject_thought_signatures(messages)

    extra = messages[0]["tool_calls"][0]["extra_content"]
    assert isinstance(extra, dict)
    assert extra["google"]["thought_signature"] == DUMMY_THOUGHT_SIGNATURE


# ---------------------------------------------------------------------------
# dispatch_gemini_messages
# ---------------------------------------------------------------------------

BASE_URL = "https://gemini.example/v1beta/openai"


def _model(model_id: str = "gemini-2.5-flash") -> Model:
    return Model(
        id=model_id,
        name=model_id,
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
        pricing=Pricing(prompt=0.0, completion=0.0),
    )


def _sse(*chunks: dict | str) -> str:
    return "".join(
        f"data: {json.dumps(c)}\n\n" if isinstance(c, dict) else f"{c}\n"
        for c in chunks
    )


async def _dispatch(
    sse: str, body: dict[str, Any] | None = None
) -> tuple[list[dict], dict[str, Any]]:
    """Dispatch a streaming request; return Anthropic events and wire body."""
    request = {
        "model": "gemini-2.5-flash",
        "max_tokens": 64,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        **(body or {}),
    }
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=sse
            )
        )
        _, result, _ = await gemini_messages.dispatch_gemini_messages(
            request_body=json.dumps(request).encode(),
            model_obj=_model(),
            base_url=BASE_URL,
            api_key="g-key",
            transform_model_name=lambda name: name,
        )
        events: list[dict] = []
        buffer = b""
        async for chunk in result:
            parsed, buffer = events_from_chunk(chunk, buffer)
            events.extend(parsed)
    return events, json.loads(route.calls.last.request.content)


@pytest.mark.asyncio
async def test_dispatch_injects_signatures_and_disables_thinking() -> None:
    history = [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a"}],
        },
    ]
    _, wire = await _dispatch(
        _sse("data: [DONE]"),
        {"messages": history, "thinking": {"type": "enabled", "budget_tokens": 2048}},
    )

    call = wire["messages"][1]["tool_calls"][0]
    assert call["extra_content"]["google"]["thought_signature"] == (
        DUMMY_THOUGHT_SIGNATURE
    )
    assert wire["reasoning_effort"] == "none"
    assert wire["stream"] is True
    assert wire["stream_options"] == {"include_usage": True}
    assert "thinking" not in wire


@pytest.mark.asyncio
async def test_translator_emits_text_only_response() -> None:
    """Plain text response: message_start → content_block_* (text) →
    message_delta(end_turn) → message_stop."""
    events, _ = await _dispatch(
        _sse(
            {
                "id": "chatcmpl-1",
                "model": "gemini-2.5-flash",
                "choices": [{"index": 0, "delta": {"role": "assistant"}}],
            },
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": ", world"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            },
        )
    )

    types = [e["type"] for e in events]
    assert types == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[0]["message"]["model"] == "gemini-2.5-flash"
    text_deltas = [
        e["delta"]["text"] for e in events if e["type"] == "content_block_delta"
    ]
    assert "".join(text_deltas) == "Hello, world"
    msg_delta = next(e for e in events if e["type"] == "message_delta")
    assert msg_delta["delta"]["stop_reason"] == "end_turn"
    assert msg_delta["usage"]["input_tokens"] == 5
    assert msg_delta["usage"]["output_tokens"] == 7


@pytest.mark.asyncio
async def test_translator_emits_tool_use_block() -> None:
    """tool_calls split across deltas → tool_use content block with
    accumulated input_json_delta and stop_reason='tool_use'."""
    events, _ = await _dispatch(
        _sse(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-abc",
                                    "type": "function",
                                    "function": {
                                        "name": "Bash",
                                        "arguments": '{"cmd":',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ' "ls"}'}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
    )

    cb_start = next(e for e in events if e["type"] == "content_block_start")
    assert cb_start["content_block"]["type"] == "tool_use"
    assert cb_start["content_block"]["name"] == "Bash"
    assert cb_start["content_block"]["id"] == "call-abc"
    deltas = [e for e in events if e["type"] == "content_block_delta"]
    assert all(d["delta"]["type"] == "input_json_delta" for d in deltas)
    assert "".join(d["delta"]["partial_json"] for d in deltas) == '{"cmd": "ls"}'
    msg_delta = next(e for e in events if e["type"] == "message_delta")
    assert msg_delta["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_translator_handles_done_sentinel_and_blank_lines() -> None:
    """``data: [DONE]`` ends the stream; comments and invalid JSON are
    skipped."""
    events, _ = await _dispatch(
        _sse(
            {"id": "x", "model": "m", "choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "ok"}}]},
            ": comment\n",
            "data: not-json\n",
            "data: [DONE]\n",
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        )
    )

    assert events[0]["type"] == "message_start"
    assert events[-1]["type"] == "message_stop"
    text = "".join(
        e["delta"]["text"] for e in events if e["type"] == "content_block_delta"
    )
    assert text == "ok"
    msg_delta = next(e for e in events if e["type"] == "message_delta")
    assert msg_delta["delta"]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_upstream_error_status_is_preserved() -> None:
    with respx.mock(assert_all_called=True) as router:
        router.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                400, json={"error": {"message": "bad request", "code": 400}}
            )
        )
        with pytest.raises(UpstreamError) as exc_info:
            await gemini_messages.dispatch_gemini_messages(
                request_body=json.dumps(
                    {"max_tokens": 8, "messages": [{"role": "user", "content": "x"}]}
                ).encode(),
                model_obj=_model(),
                base_url=BASE_URL,
                api_key="g-key",
                transform_model_name=lambda name: name,
            )
    assert exc_info.value.status_code == 400
    assert exc_info.value.message == "Upstream error: bad request"


# ---------------------------------------------------------------------------
# Response lifetime
# ---------------------------------------------------------------------------


class _TrackingStream(httpx.AsyncByteStream):
    def __init__(
        self,
        *chunks: bytes,
        error: Exception | None = None,
        started: asyncio.Event | None = None,
    ) -> None:
        self._chunks = chunks
        self._error = error
        self._started = started
        self.close_count = 0

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        if self._started is not None:
            self._started.set()
            await asyncio.Event().wait()
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def aclose(self) -> None:
        self.close_count += 1


def _owned_events(response: httpx.Response) -> ResponseOwnedIterator:
    return ResponseOwnedIterator(response.aiter_bytes(), response)


@pytest.mark.asyncio
async def test_response_owner_closes_once_after_normal_completion() -> None:
    stream = _TrackingStream(b"data: {}\n\n")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://gemini.example/chat/completions"),
        stream=stream,
    )

    assert [event async for event in _owned_events(response)]
    assert response.is_closed
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_response_owner_closes_once_after_body_failure() -> None:
    stream = _TrackingStream(error=RuntimeError("upstream body failed"))
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://gemini.example/chat/completions"),
        stream=stream,
    )

    with pytest.raises(RuntimeError, match="upstream body failed"):
        await _owned_events(response).__anext__()

    assert response.is_closed
    assert stream.close_count == 1


@pytest.mark.asyncio
async def test_response_owner_closes_once_after_cancellation() -> None:
    started = asyncio.Event()
    stream = _TrackingStream(started=started)
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://gemini.example/chat/completions"),
        stream=stream,
    )
    task = asyncio.create_task(_owned_events(response).__anext__())
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert response.is_closed
    assert stream.close_count == 1


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


async def _dispatch_with_client(client: Any) -> None:
    with patch(
        "routstr.upstream.messages_transport.acquire_upstream_http_client",
        **client,
    ):
        await gemini_messages.dispatch_gemini_messages(
            request_body=json.dumps(
                {"max_tokens": 8, "messages": [{"role": "user", "content": "x"}]}
            ).encode(),
            model_obj=_model(),
            base_url=BASE_URL,
            api_key="g-key",
            transform_model_name=lambda name: name,
        )


@pytest.mark.asyncio
async def test_pool_timeout_maps_to_503() -> None:
    client = MagicMock()
    client.timeout = httpx.Timeout(10.0)
    client.build_request = MagicMock(return_value=MagicMock())
    client.send = AsyncMock(side_effect=httpx.PoolTimeout("pool busy"))

    with pytest.raises(UpstreamError) as exc_info:
        await _dispatch_with_client({"return_value": client})
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_shutdown_surfaces_as_503() -> None:
    with pytest.raises(UpstreamError) as exc_info:
        await _dispatch_with_client(
            {"side_effect": UpstreamError("shutting down", status_code=503)}
        )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_safety_block_is_a_refusal() -> None:
    """The old Gemini translator reported ``content_filter`` as ``refusal``."""
    events, _ = await _dispatch(
        _sse(
            {"id": "x", "model": "m", "choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {}, "finish_reason": "content_filter"}]},
        )
    )
    msg_delta = next(e for e in events if e["type"] == "message_delta")
    assert msg_delta["delta"]["stop_reason"] == "refusal"
