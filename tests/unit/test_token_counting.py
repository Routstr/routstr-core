"""Parity with litellm 1.101.2: every expected number was produced by litellm."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any
from unittest.mock import patch

import pytest
from PIL import Image

from routstr.upstream.openai import _rejects_max_tokens
from routstr.upstream.token_counting import (
    _LOAD_FAILURES,
    _encoding,
    _load_encoding,
    _read_encoding,
    count_message_tokens,
    count_text_tokens,
    image_tokens_high_detail,
)

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the weather",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "unit": {"type": "string", "enum": ["c", "f"]},
                "days": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["city"],
        },
    }
]

ANTHROPIC_TURNS = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "Look:"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
            },
        ],
    },
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Let me check.", "signature": "s"},
            {
                "type": "tool_use",
                "id": "t1",
                "name": "get_weather",
                "input": {"city": "Paris"},
            },
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": [{"type": "text", "text": "Sunny, 21C"}],
            }
        ],
    },
]

OPENAI_TOOL_TURNS = [
    {
        "role": "assistant",
        "content": None,
        "name": "bot",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "f", "arguments": '{"q": "x"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": "result text"},
]


@pytest.mark.parametrize(
    ("model", "messages", "tools", "expected"),
    [
        ("gpt-4o", [{"role": "user", "content": "hello world"}], None, 9),
        (
            "deepseek/deepseek-chat",
            [{"role": "user", "content": "hello world"}],
            None,
            9,
        ),
        (
            "claude-3-5-sonnet-20241022",
            [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Weather in Paris?"},
            ],
            TOOLS,
            67,
        ),
        ("anthropic/claude-sonnet-4.5", ANTHROPIC_TURNS, TOOLS, 173),
        ("gpt-4.1", OPENAI_TOOL_TURNS, None, 23),
        (
            "gpt-3.5-turbo-0301",
            [{"role": "user", "name": "ann", "content": "hi"}],
            None,
            10,
        ),
    ],
)
def test_message_tokens_match_litellm(
    model: str, messages: list[dict[str, Any]], tools: Any, expected: int
) -> None:
    assert count_message_tokens(model, messages, tools) == expected


@pytest.mark.parametrize(
    ("model", "text", "expected"),
    [
        ("gpt-4o", "The quick brown fox " * 100, 402),
        ("gpt-5", "a" * 5000, 625),
        ("qwen/qwen3-coder", "<|endoftext|> 你好世界 " * 300, 3603),
    ],
)
def test_text_tokens_match_litellm(model: str, text: str, expected: int) -> None:
    assert count_text_tokens(model, text) == expected


@pytest.mark.parametrize(
    "block",
    [
        {"type": "redacted_thinking", "data": "x"},
        {"type": "tool_result", "content": "missing tool_use_id"},
        {"text": "missing type"},
    ],
)
def test_unsupported_blocks_raise_like_litellm(block: dict[str, Any]) -> None:
    with pytest.raises((KeyError, ValueError)):
        count_message_tokens("gpt-4o", [{"role": "user", "content": [block]}])


@pytest.mark.parametrize(
    ("fmt", "width", "height", "expected"),
    [
        ("PNG", 300, 200, 255),
        ("JPEG", 1500, 700, 1445),
        ("GIF", 600, 1300, 1445),
        ("WEBP", 769, 100, 765),
        ("PNG", 1024, 1024, 765),
    ],
)
def test_high_detail_image_tokens_match_litellm(
    fmt: str, width: int, height: int, expected: int
) -> None:
    buf = BytesIO()
    Image.new("RGB", (width, height)).save(buf, fmt)
    url = f"data:image/{fmt.lower()};base64,{base64.b64encode(buf.getvalue()).decode()}"
    assert image_tokens_high_detail(url) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5", True),
        ("openai/gpt-5-mini", True),
        ("gpt-5-chat-latest", False),
        ("gpt-6", True),
        ("o3", True),
        ("openai/o4-mini", True),
        ("o9-unknown", False),
        ("gpt-4o", False),
        ("omni", False),
    ],
)
def test_reasoning_model_detection_matches_litellm(model: str, expected: bool) -> None:
    assert _rejects_max_tokens(model) is expected


@pytest.mark.parametrize(
    ("model", "encoding"),
    [("gpt-4o", "o200k_base"), ("deepseek/deepseek-chat", "cl100k_base")],
)
def test_encodings_load_from_vendored_files_without_network(
    model: str, encoding: str
) -> None:
    _encoding.cache_clear()
    _read_encoding.cache_clear()
    with patch("requests.get", side_effect=AssertionError("downloaded")):
        assert _encoding(model).name == encoding
        assert count_text_tokens(model, "hello world") == 2


def test_unvendored_encoding_falls_back_instead_of_downloading() -> None:
    _encoding.cache_clear()
    with (
        patch("tiktoken.model.encoding_name_for_model", return_value="o200k_harmony"),
        patch("requests.get", side_effect=AssertionError("downloaded")),
    ):
        assert _encoding("gpt-4.1").name == "o200k_base"
    _encoding.cache_clear()


def test_broken_vendored_encoding_fails_fast_after_first_try() -> None:
    _read_encoding.cache_clear()
    reads = 0

    def broken(_path: object) -> bytes:
        nonlocal reads
        reads += 1
        raise OSError("missing")

    try:
        with patch("pathlib.Path.read_bytes", broken):
            for _ in range(3):
                with pytest.raises(OSError):
                    _load_encoding("cl100k_base")
        assert reads == 1
    finally:
        _LOAD_FAILURES.clear()
        _read_encoding.cache_clear()
