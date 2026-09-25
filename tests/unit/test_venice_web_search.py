"""Venice web search over ``/v1/messages``.

litellm's Anthropic adapter rewrites an Anthropic server-side web-search tool
into a top-level ``web_search_options``, which Venice rejects with
``400 Unrecognized key(s) in object: 'web_search_options'``. These tests pin
the trade: the tool is lifted out of the body and the same intent re-expressed
as a Venice model feature suffix.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

from routstr.core.exceptions import UpstreamError
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.venice import VeniceUpstreamProvider

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
FUNCTION_TOOL = {
    "name": "lookup",
    "description": "Look something up",
    "input_schema": {"type": "object", "properties": {}},
}


def _model(model_id: str = "deepseek-v4-flash-0731") -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="Unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.0, completion=0.0),
    )


def _body(**extra: Any) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "what shipped today?"}],
        "max_tokens": 64,
        **extra,
    }


async def _dispatch(provider: BaseUpstreamProvider, body: dict[str, Any]) -> dict:
    """Run the real dispatcher, capturing the kwargs litellm would receive."""
    captured: dict[str, Any] = {}

    async def empty_iter() -> AsyncIterator[dict]:
        if False:
            yield {}

    async def fake_acreate(**kwargs: Any) -> AsyncIterator[dict]:
        captured.update(kwargs)
        return empty_iter()

    with patch(
        "litellm.anthropic.messages.acreate",
        new=AsyncMock(side_effect=fake_acreate),
    ):
        await provider._dispatch_anthropic_messages(
            request_body=json.dumps(
                {"model": "venice/x", "stream": True, **body}
            ).encode(),
            model_obj=_model(),
        )
    return captured


@pytest.mark.asyncio
async def test_web_search_tool_never_reaches_venice_as_web_search_options() -> None:
    """The reported 400: the derived parameter must not be sent at all."""
    provider = VeniceUpstreamProvider(api_key="sk-test")

    kwargs = await _dispatch(provider, _body(tools=[WEB_SEARCH_TOOL]))

    assert "web_search_options" not in kwargs
    assert "tools" not in kwargs
    assert kwargs["model"] == (
        "openai/deepseek-v4-flash-0731:enable_web_search=auto&enable_web_citations=true"
    )
    assert kwargs["api_base"] == "https://api.venice.ai/api/v1"


@pytest.mark.asyncio
async def test_function_tools_survive_alongside_web_search() -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")

    kwargs = await _dispatch(
        provider,
        _body(
            tools=[WEB_SEARCH_TOOL, FUNCTION_TOOL],
            tool_choice={"type": "tool", "name": "lookup"},
        ),
    )

    assert kwargs["tools"] == [FUNCTION_TOOL]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "lookup"}
    assert "web_search_options" not in kwargs
    assert kwargs["model"].endswith(":enable_web_search=auto&enable_web_citations=true")


@pytest.mark.asyncio
async def test_requests_without_web_search_are_untouched() -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")

    kwargs = await _dispatch(provider, _body(tools=[FUNCTION_TOOL]))

    assert kwargs["model"] == "openai/deepseek-v4-flash-0731"
    assert kwargs["tools"] == [FUNCTION_TOOL]


@pytest.mark.asyncio
async def test_other_providers_keep_their_existing_behaviour() -> None:
    """The base hook is a no-op, so no non-Venice upstream changes shape."""
    provider = BaseUpstreamProvider(base_url="http://test", api_key="k")

    kwargs = await _dispatch(provider, _body(tools=[WEB_SEARCH_TOOL]))

    assert kwargs["model"] == "openai/deepseek-v4-flash-0731"
    assert kwargs["tools"] == [WEB_SEARCH_TOOL]


@pytest.mark.parametrize(
    "tool",
    [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "allowed_domains": ["example.com"],
        },
        {"type": "web_search_20250305", "name": "web_search", "blocked_domains": ["x"]},
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "user_location": {"type": "approximate", "country": "DE"},
        },
    ],
)
def test_constraints_venice_cannot_enforce_are_refused(tool: dict[str, Any]) -> None:
    """Better an explicit 400 than a search that quietly ignored the limit."""
    provider = VeniceUpstreamProvider(api_key="sk-test")

    with pytest.raises(UpstreamError) as excinfo:
        provider.adapt_messages_request(_body(tools=[tool]), _model())

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "UNSUPPORTED_WEB_SEARCH_OPTION"


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": None},
        {"type": "web_search_20250305", "name": "web_search", "allowed_domains": []},
    ],
)
def test_constraint_keys_stating_nothing_are_read_as_absent(
    tool: dict[str, Any],
) -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")

    assert provider.adapt_messages_request(_body(tools=[tool]), _model()) != ""


def test_forcing_web_search_through_tool_choice_is_refused() -> None:
    provider = VeniceUpstreamProvider(api_key="sk-test")
    body = _body(
        tools=[WEB_SEARCH_TOOL],
        tool_choice={"type": "tool", "name": "web_search"},
    )

    with pytest.raises(UpstreamError) as excinfo:
        provider.adapt_messages_request(body, _model())

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "UNSUPPORTED_WEB_SEARCH_OPTION"
    assert excinfo.value.details == {"unsupported_options": ["tool_choice"]}


def test_web_search_only_request_drops_tool_choice() -> None:
    """Without tools left, a surviving tool_choice is rejected upstream."""
    provider = VeniceUpstreamProvider(api_key="sk-test")
    body = _body(tools=[WEB_SEARCH_TOOL], tool_choice={"type": "auto"})

    provider.adapt_messages_request(body, _model())

    assert "tools" not in body
    assert "tool_choice" not in body


@pytest.mark.asyncio
async def test_claude_code_web_search_tool_is_accepted() -> None:
    """Claude Code always sends ``max_uses: 8``; Venice's single ``auto``
    search already stays under any cap of one or more."""
    provider = VeniceUpstreamProvider(api_key="sk-test")
    tool = {
        "type": "web_search_20250305",
        "name": "web_search",
        "allowed_domains": None,
        "blocked_domains": None,
        "max_uses": 8,
    }

    kwargs = await _dispatch(provider, _body(tools=[tool]))

    assert "web_search_options" not in kwargs
    assert "tools" not in kwargs
    assert kwargs["model"] == (
        "openai/deepseek-v4-flash-0731:enable_web_search=auto&enable_web_citations=true"
    )


def test_zero_max_uses_is_refused() -> None:
    """``auto`` may still search, so a request for no search cannot be met."""
    provider = VeniceUpstreamProvider(api_key="sk-test")
    tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 0}

    with pytest.raises(UpstreamError) as excinfo:
        provider.adapt_messages_request(_body(tools=[tool]), _model())

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "UNSUPPORTED_WEB_SEARCH_OPTION"
    assert excinfo.value.details == {"unsupported_options": ["max_uses"]}


def test_tool_named_web_search_without_the_type_marker_is_caught() -> None:
    """litellm matches on either marker, so this one would also be rewritten."""
    provider = VeniceUpstreamProvider(api_key="sk-test")
    body = _body(tools=[{"name": "web_search"}])

    assert provider.adapt_messages_request(body, _model()) != ""
    assert "tools" not in body


def test_litellm_adapter_derives_no_web_search_options_from_the_adapted_body() -> None:
    """The fix at its cause: run the real litellm translation over the body
    this provider produces and assert the rejected key is never derived."""
    from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (  # noqa: E501
        LiteLLMAnthropicMessagesAdapter,
    )

    provider = VeniceUpstreamProvider(api_key="sk-test")
    adapter = LiteLLMAnthropicMessagesAdapter()  # type: ignore[no-untyped-call]
    body = _body(tools=[WEB_SEARCH_TOOL, FUNCTION_TOOL])

    def translate(request: dict[str, Any]) -> dict:
        # litellm types the request as a TypedDict; these bodies are built
        # from client JSON, so they are plain dicts at this seam.
        translated, _ = adapter.translate_anthropic_to_openai(request)  # type: ignore[arg-type]
        return dict(translated)

    # Unadapted, litellm derives the parameter Venice rejects.
    before = translate({"model": "m", **_body(tools=[WEB_SEARCH_TOOL])})
    assert "web_search_options" in before

    provider.adapt_messages_request(body, _model())

    assert "web_search_options" not in translate({"model": "m", **body})
