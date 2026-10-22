"""Per-provider wire rules for the ``/v1/messages`` bridge.

Each provider prefix (the ``provider/`` routing key from ``provider_prefixes``)
maps to the request litellm 1.101.2 built for it after translating the
Anthropic body: URL, headers, which OpenAI params survive ``drop_params``, and
provider-specific renames and message rewrites. The pipeline mirrors
``litellm.completion``: unsupported params are dropped, the survivors go
through the provider's ``map_openai_params``, then its request transform.

Prefixes whose litellm config forwards Anthropic Messages natively
(``deepseek``, ``minimax``, ``anthropic``) skip the translation;
``ollama_chat`` speaks Ollama's ``/api/chat``.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .anthropic_openai import (
    anthropic_to_openai,
    completion_request,
    hoist_tool_images,
    openai_chat_messages,
    sanitize_anthropic_messages,
    strip_cache_control,
)
from .anthropic_openai_stream import StreamPayloadError
from .chat_params import Messages, Params, normalize_reasoning_effort
from .chat_providers import chat_provider, chunk_hook, resolve_fireworks_model
from .chat_providers_extra import content_text
from .responses_bridge import needs_responses_api, responses_body, responses_url

ANTHROPIC_VERSION = "2023-06-01"
AZURE_API_VERSION = "2025-02-01-preview"

# ``litellm.get_optional_params`` treats these as OpenAI params (dropped when
# the provider does not support them); any other key in the translated
# request rides along as a provider-specific extra (e.g. ``top_k``).
_OPENAI_PARAMS = frozenset(
    {
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "stop",
        "user",
        "prompt_cache_key",
        "stream_options",
        "tools",
        "tool_choice",
        "reasoning_effort",
        "web_search_options",
        "response_format",
        "parallel_tool_calls",
    }
)
_NOT_FORWARDED = frozenset({"model", "messages", "stream", "metadata"})


@dataclass
class UpstreamCall:
    """Everything the transport and stream translator need for one call."""

    wire: str  # "openai" | "responses" | "anthropic" | "ollama"
    provider: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    model: str
    tool_name_mapping: dict[str, str] = field(default_factory=dict)
    # litellm's HTTP-handler providers mapped finish reasons while parsing;
    # the openai-SDK ones handed them on raw.
    http_handler: bool = False
    prepare_chunk: Callable[[dict], dict] | None = None
    # Chat messages for the no-usage token estimate when the body has none.
    estimate_messages: list[dict[str, Any]] | None = None
    # The old Gemini translator: safety blocks surface as ``refusal`` and
    # non-JSON events are skipped.
    refusal_on_content_filter: bool = False
    skip_malformed_events: bool = False


def provider_name(prefix: str) -> str:
    return prefix.rstrip("/")


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------


def _bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _chat_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def _keeps_cache_control(provider: str, base_url: str) -> bool:
    if provider != "openai":
        return False
    host = urlsplit(base_url).hostname
    return (
        host is not None and host != "openai.com" and not host.endswith(".openai.com")
    )


def _translate(
    prefix: str, upstream_model: str, body: dict[str, Any]
) -> tuple[Messages, Params, Params, dict[str, str]]:
    """Anthropic body -> (messages, OpenAI params, extras, tool name map)."""
    provider = provider_name(prefix)
    routing_model = f"{prefix}{upstream_model}"
    body = {**body, "messages": sanitize_anthropic_messages(body["messages"])}
    request = completion_request(body, routing_model)
    kwargs, name_mapping = anthropic_to_openai(request)
    for key, value in body.items():
        # Fields the handler passes as extra kwargs (``reasoning_effort``).
        if key not in request and key not in kwargs and value is not None:
            if key not in ("output_config", "safeguards"):
                kwargs[key] = value
    effort = kwargs.get("reasoning_effort")
    if isinstance(effort, str):
        kwargs["reasoning_effort"] = normalize_reasoning_effort(
            effort, routing_model, provider
        )
    kwargs["stream_options"] = {"include_usage": True}
    messages = kwargs.pop("messages")
    params = {k: v for k, v in kwargs.items() if k in _OPENAI_PARAMS and v is not None}
    extras = {
        k: v
        for k, v in kwargs.items()
        if k not in _OPENAI_PARAMS and k not in _NOT_FORWARDED
    }
    return messages, params, extras, name_mapping


def build_openai_call(
    *,
    prefix: str,
    base_url: str,
    api_key: str,
    upstream_model: str,
    body: dict[str, Any],
    api_version: str | None = None,
) -> UpstreamCall:
    provider = provider_name(prefix)
    messages, params, extras, name_mapping = _translate(prefix, upstream_model, body)
    model = upstream_model
    spec = chat_provider(provider, model)
    supported = spec.supported(model)
    optional = {k: v for k, v in params.items() if k in supported}
    if spec.map_params is not None:
        optional = spec.map_params(optional, model)
    if needs_responses_api(
        provider,
        model,
        base_url,
        tools=params.get("tools"),
        reasoning_effort=params.get("reasoning_effort"),
    ):
        headers = {"Content-Type": "application/json"}
        if provider == "azure":
            headers["api-key"] = api_key
        else:
            headers.update(_bearer(api_key))
        return UpstreamCall(
            wire="responses",
            provider=provider,
            url=responses_url(provider, base_url),
            headers=headers,
            body=responses_body(model, messages, optional),
            model=upstream_model,
            tool_name_mapping=name_mapping,
            http_handler=True,
            estimate_messages=messages,
        )
    if spec.transform is not None:
        messages = spec.transform(messages, optional, model)
    if provider == "azure":
        messages = hoist_tool_images(messages)
    else:
        messages = openai_chat_messages(messages)
    if spec.strip_cache_control and not _keeps_cache_control(provider, base_url):
        messages = strip_cache_control(messages)
        if optional.get("tools"):
            optional["tools"] = strip_cache_control(optional["tools"])
    if not optional.get("tools"):
        optional.pop("tool_choice", None)
    messages = [_drop_none_content(m) for m in messages]

    headers = {**_bearer(api_key), "Content-Type": "application/json"}
    url = _chat_url(base_url)
    if provider == "fireworks_ai":
        model = resolve_fireworks_model(model)
    elif provider == "azure":
        url = (
            f"{base_url}/openai/deployments/{model}/chat/completions"
            f"?api-version={api_version or AZURE_API_VERSION}"
        )
        if api_key:
            headers["api-key"] = api_key
    request_body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        **optional,
        **extras,
    }
    return UpstreamCall(
        wire="openai",
        provider=provider,
        url=url,
        headers=headers,
        body=request_body,
        model=upstream_model,
        tool_name_mapping=name_mapping,
        http_handler=not spec.sdk,
        prepare_chunk=chunk_hook(provider),
    )


def _drop_none_content(message: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in message.items() if v is not None}


# --------------------------------------------------------------------------
# Ollama /api/chat
# --------------------------------------------------------------------------

_OLLAMA_SUPPORTED = frozenset(
    {
        "max_tokens",
        "max_completion_tokens",
        "top_p",
        "temperature",
        "stop",
        "tools",
        "tool_choice",
        "response_format",
        "reasoning_effort",
    }
)
_THINK_TAGS = re.compile(
    r"<(?:think|thinking|budget:thinking)>(.*?)</(?:think|thinking|budget:thinking)>(.*)",
    re.DOTALL,
)


def _ollama_message(m: dict[str, Any]) -> dict[str, Any]:
    tool_calls = None
    if isinstance(m.get("tool_calls"), list):
        tool_calls = [
            {
                "function": {
                    "name": t["function"].get("name") or "",
                    "arguments": json.loads(t["function"]["arguments"])
                    if "arguments" in t["function"]
                    else {},
                }
            }
            for t in m["tool_calls"]
            if t.get("type", "function") == "function"
        ]
    content = m.get("content")
    if "reasoning_content" in m:
        reasoning = m["reasoning_content"]
    elif "reasoning" in m:
        reasoning = m["reasoning"]
    elif isinstance(content, str) and content and (match := _THINK_TAGS.match(content)):
        reasoning = match.group(1)
    else:
        reasoning = None
    images: list[str] = []
    if isinstance(content, list):
        for part in content:
            image_url = part.get("image_url")
            url = (
                image_url
                if isinstance(image_url, str)
                else (image_url or {}).get("url")
            )
            if url:
                if url.startswith("data:") and ";base64," in url:
                    url = url.split(";base64,", 1)[1]
                images.append(url)
    out: dict[str, Any] = {"role": m.get("role")}
    if reasoning is not None:
        out["thinking"] = reasoning
    out["content"] = content_text(m) if content else ""
    out["images"] = images
    if tool_calls is not None:
        out["tool_calls"] = tool_calls
    if m.get("tool_call_id") is not None:
        out["tool_call_id"] = m["tool_call_id"]
    return out


async def ollama_chat_chunks(lines: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Ollama ``/api/chat`` NDJSON -> OpenAI-shaped stream chunks."""
    started = finished = seen_tools = False
    async for chunk in lines:
        message = chunk.get("message")
        if not isinstance(message, dict):
            raise StreamPayloadError(
                f"KeyError: 'message', Got unexpected response from Ollama: {chunk}",
                400,
            )
        tool_calls = message.get("tool_calls")
        if tool_calls is not None:
            seen_tools = True
            for call in tool_calls:
                args = (call.get("function") or {}).get("arguments")
                if args and _complete_args(args):
                    call["id"] = str(uuid.uuid4())
        reasoning = None
        content = None
        if message.get("thinking"):
            reasoning = message["thinking"]
            started = True
        if message.get("content"):
            text = message["content"]
            if started and not finished:
                finished = True
            if "<think>" in text:
                text = text.replace("<think>", "")
                started = True
            if "</think>" in text and started:
                text = text.replace("</think>", "")
                finished = True
            if started and not finished:
                reasoning = text
            else:
                content = text
        delta: dict[str, Any] = {"content": content, "tool_calls": tool_calls}
        if reasoning is not None:
            delta["reasoning_content"] = reasoning
        finish = None
        if chunk.get("done") is True:
            finish = (
                "tool_calls" if seen_tools else (chunk.get("done_reason") or "stop")
            )
        prompt = chunk.get("prompt_eval_count", 0)
        completion = chunk.get("eval_count", 0)
        yield {
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }


def _complete_args(args: Any) -> bool:
    if isinstance(args, dict):
        return True
    try:
        json.loads(args)
    except (TypeError, ValueError):
        return False
    return True


def build_ollama_call(
    *,
    prefix: str,
    base_url: str,
    api_key: str,
    upstream_model: str,
    body: dict[str, Any],
) -> UpstreamCall:
    messages, params, extras, name_mapping = _translate(prefix, upstream_model, body)
    model = upstream_model
    options: Params = {}
    request: dict[str, Any] = {}
    for key, value in params.items():
        if key not in _OLLAMA_SUPPORTED:
            continue
        if key in ("max_tokens", "max_completion_tokens"):
            options["num_predict"] = value
        elif key in ("temperature", "top_p", "stop"):
            options[key] = value
        elif key == "response_format" and isinstance(value, dict):
            if value.get("type") == "json_object":
                request["format"] = "json"
            elif value.get("type") == "json_schema" and (
                value.get("json_schema") or {}
            ).get("schema"):
                request["format"] = value["json_schema"]["schema"]
        elif key == "reasoning_effort":
            request["think"] = (
                value
                if model.startswith("gpt-oss")
                else value in {"low", "medium", "high"}
            )
        elif key == "tools":
            request["tools"] = value
    options.update(extras)
    messages = openai_chat_messages(messages)
    data: dict[str, Any] = {
        "model": model,
        "messages": [_ollama_message(m) for m in messages],
        "options": options,
        "stream": True,
    }
    for key in ("format", "tools", "think"):
        if key in request:
            data[key] = request[key]
    base = base_url.rstrip("/") if base_url else "http://localhost:11434"
    url = base_url if base_url.endswith("/api/chat") else f"{base_url}/api/chat"
    del base
    return UpstreamCall(
        wire="ollama",
        provider=provider_name(prefix),
        url=url,
        headers=_bearer(api_key),
        body=data,
        model=upstream_model,
        tool_name_mapping=name_mapping,
        http_handler=True,
    )


# --------------------------------------------------------------------------
# Native Anthropic Messages providers
# --------------------------------------------------------------------------

_ANTHROPIC_OPTIONAL = (
    "system",
    "stop_sequences",
    "temperature",
    "top_p",
    "top_k",
    "tools",
    "tool_choice",
    "metadata",
    "reasoning_effort",
)
_THINKING_BUDGETS = {
    "low": 1024,
    "medium": 2048,
    "high": 4096,
    "xhigh": 8192,
    "max": 16384,
    "minimal": 1024,
}
_MIN_THINKING_BUDGET = 1024
_CLAUDE_ADAPTIVE = re.compile(
    r"claude-[a-z]+-(?:4[-._](?:[6-9]|[1-9]\d)(?!\d)|(?:[5-9]|[1-9]\d)(?!\d)(?:[-._]\d{1,2}(?!\d))?)"
)
NATIVE_ANTHROPIC_PROVIDERS = frozenset({"anthropic", "deepseek", "minimax"})


def _anthropic_url(provider: str, base_url: str) -> str:
    if provider == "deepseek":
        base = base_url.rstrip("/")
        if base.endswith("/v1/messages") and "/anthropic/" in base:
            return base
        base = (
            base.removesuffix("/v1/messages").removesuffix("/v1").removesuffix("/beta")
        )
        if not base.endswith("/anthropic") and "/anthropic/" not in base:
            base = f"{base}/anthropic"
        return f"{base}/v1/messages"
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    # A base that already names the API version (``https://api.anthropic.com/v1``)
    # must not become ``/v1/v1/messages`` (litellm's 404).
    return f"{base.removesuffix('/v1')}/v1/messages"


def _strip_billing_header(system: Any) -> Any:
    if isinstance(system, str):
        return None if system.startswith("x-anthropic-billing-header:") else system
    if isinstance(system, list):
        kept = [
            b
            for b in system
            if not (
                isinstance(b, dict)
                and b.get("type", "") == "text"
                and str(b.get("text", "")).startswith("x-anthropic-billing-header:")
            )
        ]
        return kept or None
    return system


def _apply_reasoning_effort(
    optional: Params, model: str, max_tokens: int | None
) -> None:
    """litellm ``_translate_reasoning_effort_to_anthropic``."""
    effort = optional.pop("reasoning_effort", None)
    if not isinstance(effort, str):
        return
    if effort == "none":
        optional.pop("thinking", None)
        optional.pop("output_config", None)
        return
    adaptive = bool(_CLAUDE_ADAPTIVE.search(model))
    if adaptive:
        thinking: Params = {"type": "adaptive", "display": "summarized"}
    else:
        budget = _THINKING_BUDGETS.get(effort)
        if budget is None:
            raise ValueError(
                f"Unmapped reasoning effort: {effort!r}. Must be one of: 'minimal', "
                "'low', 'medium', 'high', 'xhigh', 'max', 'none'."
            )
        thinking = {"type": "enabled", "budget_tokens": budget}
        if max_tokens is not None:
            if max_tokens <= _MIN_THINKING_BUDGET:
                return
            if budget >= max_tokens:
                thinking = {"type": "enabled", "budget_tokens": max_tokens - 1}
    optional.setdefault("thinking", thinking)
    if adaptive:
        config = optional.get("output_config")
        config = dict(config) if isinstance(config, dict) else {}
        config.setdefault("effort", "low" if effort == "minimal" else effort)
        optional["output_config"] = config


def build_anthropic_call(
    *,
    prefix: str,
    base_url: str,
    api_key: str,
    upstream_model: str,
    body: dict[str, Any],
) -> UpstreamCall:
    provider = provider_name(prefix)
    messages = sanitize_anthropic_messages(body["messages"])
    # Shallow: values are replaced below, never mutated in place.
    optional = {
        k: body[k] for k in _ANTHROPIC_OPTIONAL if k in body and body[k] is not None
    }
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        raise ValueError("max_tokens is required for Anthropic /v1/messages API")
    _apply_reasoning_effort(optional, upstream_model, max_tokens)
    thinking = optional.get("thinking")
    temperature = optional.get("temperature")
    if (
        not _CLAUDE_ADAPTIVE.search(upstream_model)
        and temperature is not None
        and temperature != 1
        and isinstance(thinking, dict)
        and thinking.get("type") == "enabled"
    ):
        optional.pop("temperature")
    if provider in ("deepseek", "minimax") and "system" in optional:
        system = _strip_billing_header(optional["system"])
        if system:
            optional["system"] = system
        else:
            optional.pop("system")
    if provider == "deepseek" and isinstance(optional.get("tools"), list):
        optional["tools"] = [
            {k: v for k, v in t.items() if k != "type"}
            if isinstance(t, dict) and t.get("type") == "custom"
            else t
            for t in optional["tools"]
        ]
    headers = {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if api_key:
        headers["x-api-key"] = api_key
    return UpstreamCall(
        wire="anthropic",
        provider=provider,
        url=_anthropic_url(provider, base_url),
        headers=headers,
        body={
            "messages": messages,
            "max_tokens": max_tokens,
            "model": upstream_model,
            **optional,
            "stream": True,
        },
        model=upstream_model,
    )


def build_upstream_call(
    *,
    prefix: str,
    base_url: str,
    api_key: str,
    upstream_model: str,
    body: dict[str, Any],
    api_version: str | None = None,
) -> UpstreamCall:
    """``api_version``: the provider row's Azure API version, if configured."""
    provider = provider_name(prefix)
    kwargs: dict[str, Any] = {
        "prefix": prefix,
        "base_url": base_url,
        "api_key": api_key,
        "upstream_model": upstream_model,
        "body": body,
    }
    if provider in NATIVE_ANTHROPIC_PROVIDERS:
        return build_anthropic_call(**kwargs)
    if provider == "ollama_chat":
        return build_ollama_call(**kwargs)
    return build_openai_call(**kwargs, api_version=api_version)
