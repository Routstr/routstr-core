from __future__ import annotations

import pytest

from routstr.upstream.venice import VeniceUpstreamProvider

from .test_venice_web_search import _body, _dispatch

EPHEMERAL = {"type": "ephemeral"}

CLAUDE_CODE_SYSTEM = [
    {
        "type": "text",
        "text": "x-anthropic-billing-header: cc_version=2.1.281; cc_entrypoint=cli;",
    },
    {"type": "text", "text": "You are a Claude agent.", "cache_control": EPHEMERAL},
    {
        "type": "text",
        "text": "\nYou are an interactive agent.",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    },
]


@pytest.mark.asyncio
async def test_cache_marked_multi_block_system_is_merged_into_one_block() -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")

    kwargs = await _dispatch(provider, _body(system=CLAUDE_CODE_SYSTEM))

    assert kwargs["system"] == [
        {
            "type": "text",
            "text": (
                "x-anthropic-billing-header: cc_version=2.1.281; cc_entrypoint=cli;"
                "\n\nYou are a Claude agent.\n\n\nYou are an interactive agent."
            ),
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]


@pytest.mark.asyncio
async def test_unmarked_multi_block_system_is_untouched() -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")
    system = [{"type": "text", "text": "A."}, {"type": "text", "text": "B."}]

    kwargs = await _dispatch(provider, _body(system=system))

    assert kwargs["system"] == system


@pytest.mark.asyncio
async def test_single_marked_block_and_string_system_are_untouched() -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")
    single = [{"type": "text", "text": "A.", "cache_control": EPHEMERAL}]

    assert (await _dispatch(provider, _body(system=single)))["system"] == single
    assert (await _dispatch(provider, _body(system="A.")))["system"] == "A."


@pytest.mark.asyncio
async def test_message_and_tool_cache_markers_are_kept() -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hi", "cache_control": EPHEMERAL}],
        }
    ]
    tools = [
        {
            "name": "Bash",
            "description": "Run a command",
            "input_schema": {"type": "object", "properties": {}},
            "cache_control": EPHEMERAL,
        }
    ]

    kwargs = await _dispatch(
        provider,
        _body(system=CLAUDE_CODE_SYSTEM, messages=messages, tools=tools),
    )

    assert kwargs["messages"] == messages
    assert kwargs["tools"] == tools
