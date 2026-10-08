import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from routstr.upstream import messages_dispatch
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.venice import VeniceUpstreamProvider, _drop_encrypted_reasoning

from .test_venice_web_search import _model

ENCRYPTED = "__ENCRYPTED_REASONING__id=rs_0b04\ngAAAAABqvDJD"


def _block(index: int, block: dict, deltas: list[dict]) -> list[dict]:
    return [
        {"type": "content_block_start", "index": index, "content_block": block},
        *({"type": "content_block_delta", "index": index, "delta": d} for d in deltas),
        {"type": "content_block_stop", "index": index},
    ]


def _thinking(index: int, text: str) -> list[dict]:
    return _block(
        index,
        {"type": "thinking", "thinking": "", "signature": ""},
        [{"type": "thinking_delta", "thinking": text}],
    )


def _text(index: int, text: str) -> list[dict]:
    return _block(
        index,
        {"type": "text", "text": ""},
        [{"type": "text_delta", "text": text}],
    )


def _tool(index: int) -> list[dict]:
    return _block(
        index,
        {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}},
        [{"type": "input_json_delta", "partial_json": '{"command":"ls"}'}],
    )


def _message(blocks: list[dict], stop_reason: str = "end_turn") -> list[dict]:
    return [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "role": "assistant", "content": []},
        },
        *blocks,
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}},
        {"type": "message_stop"},
    ]


async def _upstream(events: list[dict], *, split: bool = False) -> AsyncIterator[Any]:
    payload = b"".join(messages_dispatch.encode_sse(e) for e in events)
    if split:
        for i in range(0, len(payload), 7):
            yield payload[i : i + 7]
    else:
        yield payload


async def _filtered(events: list[dict], **kwargs: Any) -> list[dict]:
    buffer = b""
    out: list[dict] = []
    async for chunk in _drop_encrypted_reasoning(_upstream(events, **kwargs)):
        parsed, buffer = messages_dispatch.events_from_chunk(chunk, buffer)
        out.extend(parsed)
    return out


def _starts(events: list[dict]) -> list[tuple[int, str]]:
    return [
        (e["index"], e["content_block"]["type"])
        for e in events
        if e["type"] == "content_block_start"
    ]


@pytest.mark.asyncio
async def test_trailing_encrypted_reasoning_is_dropped() -> None:
    events = _message([*_text(0, "a.txt contains: hello"), *_thinking(1, ENCRYPTED)])

    out = await _filtered(events)

    assert _starts(out) == [(0, "text")]
    assert all(ENCRYPTED not in json.dumps(e) for e in out)
    assert out[-2]["delta"]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_leading_encrypted_reasoning_closes_index_gap() -> None:
    events = _message(
        [*_thinking(0, ENCRYPTED), *_text(1, "hi"), *_tool(2)], "tool_use"
    )

    out = await _filtered(events, split=True)

    assert _starts(out) == [(0, "text"), (1, "tool_use")]
    assert {e["index"] for e in out if "index" in e} == {0, 1}


@pytest.mark.asyncio
async def test_plaintext_thinking_is_kept_in_order() -> None:
    events = _message([*_thinking(0, "Let me list files."), *_tool(1)], "tool_use")

    out = await _filtered(events)

    assert out == events


@pytest.mark.asyncio
async def test_thinking_start_without_delta_is_flushed() -> None:
    events = _message(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {"type": "content_block_stop", "index": 0},
            *_text(1, "ok"),
        ]
    )

    out = await _filtered(events)

    assert out == events


@pytest.mark.asyncio
async def test_aggregated_message_ends_with_answer_text() -> None:
    events = _message([*_text(0, "hello"), *_thinking(1, ENCRYPTED)])

    message = await messages_dispatch.aggregate_anthropic_events_to_message(
        _drop_encrypted_reasoning(_upstream(events))
    )

    assert [b["type"] for b in message["content"]] == ["text"]
    assert message["content"][0]["text"] == "hello"


async def _dispatched_blocks(
    provider: BaseUpstreamProvider, *, stream: bool
) -> list[str]:
    events = _message([*_text(0, "hello"), *_thinking(1, ENCRYPTED)])
    with patch(
        "litellm.anthropic.messages.acreate",
        new=AsyncMock(return_value=_upstream(events)),
    ):
        _, result, _ = await provider._dispatch_anthropic_messages(
            request_body=json.dumps(
                {
                    "model": "x",
                    "stream": stream,
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}],
                }
            ).encode(),
            model_obj=_model(),
        )
    if not stream:
        return [b["type"] for b in result["content"]]
    buffer = b""
    out: list[dict] = []
    async for chunk in result:
        parsed, buffer = messages_dispatch.events_from_chunk(chunk, buffer)
        out.extend(parsed)
    return [t for _, t in _starts(out)]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_venice_dispatch_drops_encrypted_reasoning(stream: bool) -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")

    assert await _dispatched_blocks(provider, stream=stream) == ["text"]


@pytest.mark.asyncio
async def test_other_providers_keep_thinking_blocks() -> None:
    provider = BaseUpstreamProvider(base_url="https://example.com/v1", api_key="k")

    assert await _dispatched_blocks(provider, stream=True) == ["text", "thinking"]


@pytest.mark.asyncio
async def test_closing_the_filter_closes_upstream() -> None:
    closed = False

    async def upstream() -> AsyncIterator[bytes]:
        nonlocal closed
        try:
            for event in _message(_text(0, "hello")):
                yield messages_dispatch.encode_sse(event)
        finally:
            closed = True

    filtered = _drop_encrypted_reasoning(upstream())
    await filtered.__anext__()
    await filtered.aclose()

    assert closed
