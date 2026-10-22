"""Chat-completions -> OpenAI Responses API bridge.

OpenAI rejects function tools on ``/v1/chat/completions`` while reasoning is
on for gpt-5.4 and later, so litellm 1.101.2 sent those requests (and models
the map lists in ``responses`` mode) to ``/v1/responses`` instead. This ports
that bridge (``completion_extras/litellm_responses_transformation``): the
chat request it would have sent becomes a Responses body, and the Responses
event stream becomes chat chunks for ``anthropic_openai_stream``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import urlsplit

from . import model_capabilities as caps
from .anthropic_openai_stream import StreamPayloadError

OPENAI_API_HOST = "api.openai.com"
AZURE_RESPONSES_API_VERSION = "preview"

_RESPONSES_PARAMS = frozenset(
    {
        "background",
        "context_management",
        "include",
        "instructions",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "parallel_tool_calls",
        "partial_images",
        "previous_response_id",
        "prompt",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "reasoning",
        "safety_identifier",
        "service_tier",
        "store",
        "stream",
        "stream_options",
        "temperature",
        "text",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "truncation",
        "user",
    }
)
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "default"}
)
_ERROR_CODE_STATUS = {
    "server_error": 500,
    "rate_limit_exceeded": 429,
    "insufficient_quota": 429,
    "vector_store_timeout": 504,
    "invalid_prompt": 400,
    "invalid_image": 400,
    "invalid_image_format": 400,
    "invalid_base64_image": 400,
    "invalid_image_url": 400,
    "image_too_large": 400,
    "image_too_small": 400,
    "image_parse_error": 400,
    "image_content_policy_violation": 400,
    "invalid_image_mode": 400,
    "image_file_too_large": 400,
    "unsupported_image_media_type": 400,
    "empty_image_file": 400,
    "failed_to_download_image": 400,
    "image_file_not_found": 400,
    "invalid_request_error": 400,
    "context_length_exceeded": 400,
    "content_policy_violation": 400,
    "model_not_found": 400,
}


def is_gpt5_4_plus(model: str) -> bool:
    name = model.split("/")[-1]
    if name.startswith("gpt-6"):
        return True
    if not name.startswith("gpt-5."):
        return False
    try:
        return int(name.replace("gpt-5.", "").split("-")[0].split(".")[0]) >= 4
    except ValueError:
        return False


def needs_responses_api(
    provider: str,
    model: str,
    base_url: str,
    *,
    tools: list[dict] | None,
    reasoning_effort: Any,
) -> bool:
    """litellm ``responses_api_bridge_check`` for the inputs this bridge sends."""
    if provider not in ("openai", "azure"):
        return False
    info = caps.model_info(model.removeprefix("responses/"), provider)
    if info is not None and info.get("mode") == "responses":
        return True
    name = model.split("/")[-1]
    if not (
        any(m in model for m in ("gpt-5", "gpt-6"))
        and not name.startswith("gpt-5-chat")
    ):
        return False
    if "gpt-5" in model and "search" in model:
        return False
    if not is_gpt5_4_plus(model):
        return False
    if not any(t.get("type") == "function" for t in tools or ()):
        return False
    if isinstance(reasoning_effort, dict):
        active = (
            reasoning_effort.get("effort") != "none"
            or reasoning_effort.get("summary") is not None
        )
    else:
        active = reasoning_effort != "none"
    host = urlsplit(base_url).hostname
    enforcing = provider == "azure" or (
        host is not None
        and (host == OPENAI_API_HOST or host.endswith(f".{OPENAI_API_HOST}"))
    )
    return active and (reasoning_effort is not None or enforcing)


def responses_url(provider: str, base_url: str) -> str:
    if provider == "azure":
        return (
            f"{base_url.rstrip('/')}/openai/v1/responses"
            f"?api-version={AZURE_RESPONSES_API_VERSION}"
        )
    return f"{base_url.rstrip('/')}/responses"


# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------


def _text_part(text: str, role: str) -> dict[str, Any]:
    kind = (
        "input_text"
        if role in ("user", "system", "developer", "tool")
        else "output_text"
    )
    return {"type": kind, "text": text}


def _image_part(part: dict) -> dict[str, Any]:
    image_url = part.get("image_url")
    detail = None
    if isinstance(image_url, dict):
        detail = image_url.get("detail")
        image_url = image_url.get("url")
    if image_url is None:
        raise ValueError(f"Invalid image URL: {part.get('image_url')}")
    return {"image_url": image_url, "detail": detail or "auto", "type": "input_image"}


_PASSTHROUGH_PARTS = frozenset(
    {
        "input_text",
        "input_image",
        "output_text",
        "refusal",
        "input_file",
        "computer_screenshot",
        "summary_text",
    }
)


def _content(content: Any, role: str) -> list[dict[str, Any]]:
    if content is None:
        return [_text_part("", role)]
    if isinstance(content, str):
        return [_text_part(content, role)]
    if not isinstance(content, list):
        return [_text_part(str(content), role)]
    parts: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            parts.append(_text_part(item, role))
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or "input_text"
        if kind == "text":
            parts.append(_text_part(item.get("text", ""), role))
        elif kind == "image_url":
            parts.append(_image_part(item))
        elif kind == "image":
            parts.append({"type": "input_image", **item})
        elif kind == "tool_reference":
            continue
        elif kind in _PASSTHROUGH_PARTS:
            parts.append(item)
        else:
            parts.append(_text_part(str(item.get("text", item)), role))
    return parts


def _reasoning_items(message: dict) -> list[dict[str, Any]]:
    stored = message.get("reasoning_items")
    if stored:
        return [
            {
                "type": "reasoning",
                "id": item.get("id") or f"rs_{id(item)}",
                "summary": item.get("summary") or [],
                **(
                    {"encrypted_content": item["encrypted_content"]}
                    if item.get("encrypted_content")
                    else {}
                ),
            }
            for item in stored
        ]
    summary = [
        {"type": "summary_text", "text": str(block.get("thinking") or "")}
        for block in message.get("thinking_blocks") or ()
        if block.get("type") == "thinking" and block.get("thinking")
    ]
    return [{"type": "reasoning", "summary": summary}] if summary else []


def _input_items(messages: list[dict]) -> tuple[list[dict[str, Any]], str | None]:
    items: list[dict[str, Any]] = []
    instructions: str | None = None
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        tool_calls = message.get("tool_calls")
        if role == "system":
            if isinstance(content, str):
                instructions = f"{instructions} {content}" if instructions else content
            else:
                items.append(
                    {
                        "type": "message",
                        "role": role,
                        "content": _content(content, role),
                    }
                )
        elif role == "tool":
            if content is None:
                output: list[dict[str, Any]] = []
            elif isinstance(content, str):
                output = [{"type": "input_text", "text": content}]
            elif isinstance(content, list):
                output = _content(content, "user")
            else:
                output = [{"type": "input_text", "text": str(content)}]
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id"),
                    "output": output,
                }
            )
        elif role == "assistant" and tool_calls and isinstance(tool_calls, list):
            items.extend(_reasoning_items(message))
            if content:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": _content(content, "assistant"),
                    }
                )
            for call in tool_calls:
                function = call.get("function")
                if not function:
                    raise ValueError(f"tool call not supported: {call}")
                item: dict[str, Any] = {"type": "function_call", "call_id": call["id"]}
                for key in ("name", "arguments"):
                    if key in function:
                        item[key] = function[key]
                items.append(item)
        elif content is not None:
            if role == "assistant":
                items.extend(_reasoning_items(message))
            items.append(
                {
                    "type": "message",
                    "role": role,
                    "content": _content(content, str(role)),
                }
            )
        elif role == "assistant":
            items.extend(_reasoning_items(message))
    return items, instructions


def _tools(tools: list[dict]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "function":
            function = tool.get("function") or {}
            converted.append(
                {
                    "name": function["name"],
                    "parameters": function.get("parameters"),
                    "strict": function.get("strict"),
                    "type": "function",
                    "description": function.get("description"),
                }
            )
        else:
            converted.append(tool)
    return converted


def _tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return choice
    kind = choice.get("type")
    if kind in ("auto", "none", "required"):
        return kind
    if kind not in ("function", "custom"):
        return choice
    name = choice.get("name")
    if not (isinstance(name, str) and name):
        nested = choice.get(kind)
        name = nested.get("name") if isinstance(nested, dict) else None
    if isinstance(name, str) and name:
        return {"type": kind, "name": name}
    return choice


def responses_body(
    model: str, messages: list[dict], params: Mapping[str, Any]
) -> dict[str, Any]:
    """The Responses request for a chat request with provider-filtered ``params``."""
    items, instructions = _input_items(messages)
    if not items and instructions is not None:
        items = [
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": instructions}],
            }
        ]
        instructions = None
    request: dict[str, Any] = {}
    if instructions:
        request["instructions"] = instructions
    for key, value in params.items():
        if value is None:
            continue
        if key in ("max_tokens", "max_completion_tokens"):
            request["max_output_tokens"] = value
        elif key == "tools":
            request["tools"] = _tools(value)
        elif key == "tool_choice":
            request["tool_choice"] = _tool_choice(value)
        elif key == "stream_options":
            if isinstance(value, Mapping) and isinstance(
                value.get("include_obfuscation"), bool
            ):
                request["stream_options"] = {
                    "include_obfuscation": value["include_obfuscation"]
                }
        elif key in _RESPONSES_PARAMS:
            request[key] = value
        elif key == "reasoning_effort":
            if isinstance(value, dict):
                request["reasoning"] = value
            elif value in _REASONING_EFFORTS:
                request["reasoning"] = {"effort": value}
        elif key == "web_search_options":
            tools = request.setdefault("tools", [])
            tools.append(
                {"type": "web_search", **(value if isinstance(value, dict) else {})}
            )
    request["stream"] = True
    body: dict[str, Any] = {"model": model, "input": items}
    for key, value in request.items():
        if key == "user" and isinstance(value, str):
            value = value[:64]
        body[key] = value
    return body


# --------------------------------------------------------------------------
# Stream
# --------------------------------------------------------------------------


def _chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}


def _tool_call_id(item: Mapping[str, Any]) -> str:
    call_id = item.get("call_id")
    if call_id and re.fullmatch(r"call_\d+", call_id) is None:
        return str(call_id)
    return str(item.get("id") or call_id or "")


def _usage(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not usage:
        return None
    details = usage.get("input_tokens_details") or usage.get("input_token_details")
    out: dict[str, Any] = {
        "prompt_tokens": usage.get("input_tokens") or 0,
        "completion_tokens": usage.get("output_tokens") or 0,
    }
    if isinstance(details, Mapping):
        out["prompt_tokens_details"] = dict(details)
    return out


def _stream_error(event: Mapping[str, Any]) -> StreamPayloadError:
    if event.get("type") == "response.failed":
        error = (event.get("response") or {}).get("error")
    else:
        error = event.get("error")
    error = error if isinstance(error, Mapping) else {}
    message = error.get("message")
    fields = [f for f in (error.get("code"), error.get("type")) if isinstance(f, str)]
    if any(f.startswith("rate_limit") or f == "insufficient_quota" for f in fields):
        status = 429
    else:
        status = next(
            (_ERROR_CODE_STATUS[f] for f in fields if f in _ERROR_CODE_STATUS), 500
        )
    return StreamPayloadError(
        str(message) if message is not None else "Response API in-stream error", status
    )


async def responses_chat_chunks(events: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Responses API stream events -> chat-completions stream chunks."""
    tool_indexes: dict[int, int] = {}

    def tool_index(output_index: int) -> int:
        return tool_indexes.setdefault(output_index, len(tool_indexes))

    async for event in events:
        kind = event.get("type")
        if kind in ("error", "response.failed"):
            raise _stream_error(event)
        if kind == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") in ("function_call", "custom_tool_call"):
                arguments = (
                    (
                        item.get("input")
                        if item.get("type") == "custom_tool_call"
                        else item.get("arguments")
                    )
                    or event.get("arguments")
                    or ""
                )
                name = item.get("name") or (
                    "custom_tool" if item.get("type") == "custom_tool_call" else ""
                )
                yield _chunk(
                    {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": _tool_call_id(item),
                                "index": tool_index(event.get("output_index", 0)),
                                "type": "function",
                                "function": {
                                    "name": name or None,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    }
                )
                continue
        elif kind in (
            "response.function_call_arguments.delta",
            "response.custom_tool_call_input.delta",
        ):
            delta = event.get("delta")
            if not delta:
                raise StreamPayloadError(
                    f"Invalid function argument delta {event}", 500
                )
            yield _chunk(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": None,
                            "index": tool_index(event.get("output_index", 0)),
                            "type": "function",
                            "function": {"name": None, "arguments": delta},
                        }
                    ],
                }
            )
            continue
        elif kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") in ("function_call", "custom_tool_call"):
                yield _chunk({"content": None})
                continue
        elif kind == "response.output_text.delta":
            if event.get("delta") is None:
                raise StreamPayloadError(f"Invalid text delta {event}", 500)
            yield _chunk({"content": event["delta"]})
            continue
        elif kind == "response.reasoning_summary_text.delta":
            if event.get("delta"):
                yield _chunk({"content": None, "reasoning_content": event["delta"]})
                continue
        elif kind in ("response.completed", "response.incomplete"):
            response = event.get("response") or {}
            if kind == "response.incomplete":
                reason = (response.get("incomplete_details") or {}).get("reason")
                finish = "content_filter" if reason == "content_filter" else "length"
            else:
                has_calls = any(
                    isinstance(item, dict)
                    and item.get("type") in ("function_call", "custom_tool_call")
                    for item in response.get("output") or ()
                )
                finish = "tool_calls" if has_calls else "stop"
            usage = _usage(response.get("usage"))
            final: dict[str, Any] = {"content": ""}
            reasoning = [
                {
                    "id": item.get("id", ""),
                    "type": "reasoning",
                    "encrypted_content": item.get("encrypted_content"),
                    "summary": item.get("summary") or [],
                }
                for item in response.get("output") or ()
                if isinstance(item, dict) and item.get("type") == "reasoning"
            ]
            if reasoning:
                final["reasoning_items"] = reasoning
            yield _chunk(final, finish, **({"usage": usage} if usage else {}))
            continue
        yield _chunk({"content": ""})
