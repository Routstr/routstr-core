"""Unit tests for the Gemini /v1/messages dispatch path.

The Gemini upstream needs special handling because its OpenAI-compat
surface rejects inbound ``functionCall`` parts that lack a
``thought_signature``. We bypass litellm + openai SDK at the wire layer
(see ``routstr/upstream/gemini_messages.py``) so we can inject Google's
documented dummy signature (``"skip_thought_signature_validator"``).

These tests cover the two pure helpers that drive the dispatcher:

* ``inject_thought_signatures`` — request-side injection
* ``_openai_chunks_to_anthropic_events`` — response-side translator
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from routstr.upstream.gemini_messages import (
    DUMMY_THOUGHT_SIGNATURE,
    _openai_chunks_to_anthropic_events,
    inject_thought_signatures,
)

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
        messages[0]["tool_calls"][0]["extra_content"]["google"][
            "thought_signature"
        ]
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
# _openai_chunks_to_anthropic_events
# ---------------------------------------------------------------------------


async def _lines(*chunks: dict | str) -> AsyncGenerator[str, None]:
    """Helper to wrap chunk dicts as SSE-style ``data:`` lines."""
    for c in chunks:
        if isinstance(c, dict):
            yield f"data: {json.dumps(c)}"
        else:
            yield c


def _parse_anthropic_sse(blocks: list[bytes]) -> list[dict]:
    """Flatten a list of Anthropic SSE byte chunks into event dicts."""
    events: list[dict] = []
    for blob in blocks:
        text = blob.decode()
        for entry in text.split("\n\n"):
            for line in entry.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].lstrip()))
    return events


@pytest.mark.asyncio
async def test_translator_emits_text_only_response() -> None:
    """Plain text response: message_start → content_block_* (text) →
    message_delta(end_turn) → message_stop."""
    chunks: list[dict] = [
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
    ]

    out = []
    async for event_bytes in _openai_chunks_to_anthropic_events(
        _lines(*chunks), requested_model="gemini-2.5-flash"
    ):
        out.append(event_bytes)

    events = _parse_anthropic_sse(out)
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
    # Text deltas concatenate to "Hello, world".
    text_deltas = [
        e["delta"]["text"]
        for e in events
        if e["type"] == "content_block_delta"
    ]
    assert "".join(text_deltas) == "Hello, world"
    # Stop reason was mapped from openai's "stop".
    msg_delta = next(e for e in events if e["type"] == "message_delta")
    assert msg_delta["delta"]["stop_reason"] == "end_turn"
    assert msg_delta["usage"]["input_tokens"] == 5
    assert msg_delta["usage"]["output_tokens"] == 7


@pytest.mark.asyncio
async def test_translator_emits_tool_use_block() -> None:
    """tool_calls split across deltas → tool_use content block with
    accumulated input_json_delta and stop_reason='tool_use'."""
    chunks: list[dict] = [
        {
            "id": "chatcmpl-2",
            "model": "gemini-2.5-flash",
            "choices": [{"delta": {"role": "assistant"}}],
        },
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
                            {
                                "index": 0,
                                "function": {"arguments": ' "ls"}'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]

    out = []
    async for event_bytes in _openai_chunks_to_anthropic_events(
        _lines(*chunks), requested_model="gemini-2.5-flash"
    ):
        out.append(event_bytes)

    events = _parse_anthropic_sse(out)
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
    # Tool use block was opened with the right name.
    cb_start = next(e for e in events if e["type"] == "content_block_start")
    assert cb_start["content_block"]["type"] == "tool_use"
    assert cb_start["content_block"]["name"] == "Bash"
    assert cb_start["content_block"]["id"] == "call-abc"
    # Argument deltas were forwarded as input_json_delta partials.
    deltas = [e for e in events if e["type"] == "content_block_delta"]
    assert all(d["delta"]["type"] == "input_json_delta" for d in deltas)
    assert "".join(d["delta"]["partial_json"] for d in deltas) == (
        '{"cmd": "ls"}'
    )
    # tool_calls finish_reason → tool_use stop_reason.
    msg_delta = next(e for e in events if e["type"] == "message_delta")
    assert msg_delta["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_translator_handles_done_sentinel_and_blank_lines() -> None:
    """Spec edge cases from openai SSE: ``data: [DONE]``, blank lines,
    invalid JSON. Translator should skip them gracefully."""
    chunks: list[dict | str] = [
        {
            "id": "x",
            "model": "m",
            "choices": [{"delta": {"role": "assistant"}}],
        },
        {"choices": [{"delta": {"content": "ok"}}]},
        "",
        ": comment",
        "data: not-json",
        "data: [DONE]",
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]

    out = []
    async for event_bytes in _openai_chunks_to_anthropic_events(
        _lines(*chunks), requested_model=None
    ):
        out.append(event_bytes)

    events = _parse_anthropic_sse(out)
    assert events[0]["type"] == "message_start"
    assert events[-1]["type"] == "message_stop"
    text = "".join(
        e["delta"]["text"]
        for e in events
        if e["type"] == "content_block_delta"
    )
    assert text == "ok"
