"""Generic upstreams get the Anthropic ``system`` as a plain string.

The translation turns a block-list ``system`` into an OpenAI system message with
array content, which some OpenAI-compatible gateways reject with ``messages[0].content:
expected string, received array``. Claude Code always sends the block form.
"""

from __future__ import annotations

import pytest

from routstr.upstream.anthropic_openai import anthropic_to_openai
from routstr.upstream.deepseek import DeepSeekUpstreamProvider
from routstr.upstream.generic import GenericUpstreamProvider

from .test_venice_system_cache import CLAUDE_CODE_SYSTEM
from .test_venice_web_search import _body, _dispatch

JOINED = (
    "x-anthropic-billing-header: cc_version=2.1.281; cc_entrypoint=cli;"
    "\n\nYou are a Claude agent.\n\n\nYou are an interactive agent."
)


def _generic() -> GenericUpstreamProvider:
    return GenericUpstreamProvider(base_url="http://gateway.test/v1", api_key="k")


@pytest.mark.asyncio
async def test_block_system_is_sent_as_one_string() -> None:
    kwargs = await _dispatch(_generic(), _body(system=CLAUDE_CODE_SYSTEM))

    assert kwargs["system"] == JOINED


@pytest.mark.asyncio
async def test_string_system_and_messages_are_untouched() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ]

    kwargs = await _dispatch(_generic(), _body(system="A.", messages=messages))

    assert kwargs["system"] == "A."
    assert kwargs["messages"] == messages


@pytest.mark.asyncio
async def test_non_text_system_block_is_left_for_upstream_to_judge() -> None:
    system = [{"type": "text", "text": "A."}, {"type": "image", "source": {}}]

    kwargs = await _dispatch(_generic(), _body(system=system))

    assert kwargs["system"] == system


@pytest.mark.asyncio
async def test_blank_blocks_are_pruned_before_joining() -> None:
    system = [{"type": "text", "text": "  "}, {"type": "text", "text": "A."}]

    kwargs = await _dispatch(_generic(), _body(system=system))

    assert kwargs["system"] == "A."


@pytest.mark.asyncio
async def test_generic_subclasses_inherit_the_join() -> None:
    provider = DeepSeekUpstreamProvider(api_key="k")

    kwargs = await _dispatch(provider, _body(system=CLAUDE_CODE_SYSTEM))

    assert kwargs["system"] == JOINED


@pytest.mark.asyncio
async def test_translation_sends_string_system_content_upstream() -> None:
    """Pins the translation the fix relies on."""
    kwargs = await _dispatch(_generic(), _body(system=CLAUDE_CODE_SYSTEM))
    request = {
        "model": "x",
        **{k: kwargs[k] for k in ("system", "messages", "max_tokens")},
    }

    openai_request, _ = anthropic_to_openai(request)

    assert openai_request["messages"][0] == {"role": "system", "content": JOINED}
