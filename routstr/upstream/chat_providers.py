"""Per-provider OpenAI chat rules for the ``/v1/messages`` bridge.

Each entry ports one litellm 1.101.2 chat config: which OpenAI params the
provider supports (dropped otherwise, as with litellm's ``drop_params``), its
``map_openai_params`` renames, and its message transforms. Several answers
depend on the vendored model map via ``model_capabilities``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..payment.model_cost import model_cost
from ..payment.usage import parse_token_count
from . import model_capabilities as caps
from .chat_params import (
    ChatProvider,
    Messages,
    Params,
    gpt_params,
    is_gpt5_model,
    is_o_series_model,
    is_openai_catalog_model,
    max_tokens_rename,
    supports_reasoning,
)
from .chat_providers_extra import CHUNK_HOOKS, EXTRA_PROVIDERS


def _o_series_supported(model: str, provider: str = "openai") -> set[str]:
    supported = gpt_params(model) | {"reasoning_effort"}
    supported.discard("top_p")
    if provider == "openai":
        if not caps.supports(model, "openai", "supports_function_calling"):
            supported -= {"tools", "tool_choice"}
        if not caps.supports(model, "openai", "supports_parallel_function_calling"):
            supported.discard("parallel_tool_calls")
        if not caps.supports(model, "openai", "supports_response_schema"):
            supported.discard("response_format")
    return supported


def _o_series_map(params: Params, model: str) -> Params:
    params = dict(params)
    out: Params = {}
    if "max_tokens" in params:
        out["max_completion_tokens"] = params.pop("max_tokens")
    if "temperature" in params:
        temperature = params.pop("temperature")
        if temperature == 1:
            out["temperature"] = temperature
    return {**out, **params}


def _gpt5_supported(model: str, lookup: str | None = None) -> set[str]:
    """``lookup`` is the cost-map name capability checks read (azure prefixes it)."""
    if "gpt-5" in model and "search" in model:
        return {
            "max_tokens",
            "max_completion_tokens",
            "stream_options",
            "web_search_options",
            "response_format",
            "user",
        }
    name = lookup or model
    supported = gpt_params(model) | {"reasoning_effort"}
    if not caps.supports(model, None, "supports_tool_choice"):
        supported.discard("tool_choice")
    supported -= {"stop", "web_search_options"}
    if not caps.supports(name, None, "supports_none_reasoning_effort"):
        supported.discard("top_p")
    return supported


def _effort_resolves_to_none(model: str, effort: Any) -> bool:
    if effort is not None:
        return effort == "none"
    default = caps.declared(model, None, "default_reasoning_effort")
    if isinstance(default, str):
        return default == "none"
    if not any("default_reasoning_effort" in e for e in model_cost().values()):
        return caps.supports(model, None, "supports_none_reasoning_effort")
    return False


def _gpt5_map(params: Params, model: str, lookup: str | None = None) -> Params:
    name = lookup or model
    params = dict(params)
    out: Params = {}
    if "gpt-5" in model and "search" in model:
        if "max_tokens" in params:
            out["max_completion_tokens"] = params.pop("max_tokens")
        return {**out, **params}
    effort = params.get("reasoning_effort")
    if effort == "xhigh" and not caps.supports(
        name, None, "supports_xhigh_reasoning_effort"
    ):
        params.pop("reasoning_effort")
    elif effort in ("minimal", "low") and caps.explicitly_disabled(
        name, None, f"supports_{effort}_reasoning_effort"
    ):
        params.pop("reasoning_effort")
    if "max_tokens" in params:
        out["max_completion_tokens"] = params.pop("max_tokens")
    supports_none = caps.supports(name, None, "supports_none_reasoning_effort")
    resolves_none = _effort_resolves_to_none(name, effort)
    if supports_none and "top_p" in params and not resolves_none:
        params.pop("top_p")
    if "temperature" in params:
        temperature = params.pop("temperature")
        if (supports_none and resolves_none) or temperature == 1:
            out["temperature"] = temperature
    return {**out, **params}


def _azure_gpt5_map(params: Params, model: str) -> Params:
    lookup = f"azure/{model.removeprefix('gpt5_series/')}"
    supports_none = caps.supports(lookup, None, "supports_none_reasoning_effort")
    if params.get("reasoning_effort") == "none" and not supports_none:
        params = {k: v for k, v in params.items() if k != "reasoning_effort"}
    result = _gpt5_map(params, model, lookup)
    if result.get("reasoning_effort") == "none" and not supports_none:
        result.pop("reasoning_effort")
    return result


def _system_as_user(messages: Messages, model: str) -> Messages:
    if caps.supports(model, "openai", "supports_system_messages"):
        return messages
    return [
        {"role": "user", "content": m["content"]} if m.get("role") == "system" else m
        for m in messages
    ]


def _openai_provider(model: str) -> ChatProvider:
    if is_o_series_model(model):
        return ChatProvider(
            supported=_o_series_supported,
            map_params=_o_series_map,
            transform=lambda messages, _p, m: _system_as_user(messages, m),
            strip_cache_control=False,
            sdk=True,
        )
    if is_gpt5_model(model):
        return ChatProvider(
            supported=_gpt5_supported,
            map_params=lambda params, m: _gpt5_map(params, m),
            strip_cache_control=False,
            sdk=True,
        )
    if is_openai_catalog_model(model):
        return ChatProvider(supported=gpt_params, strip_cache_control=False, sdk=True)
    return ChatProvider(
        supported=lambda m: gpt_params(m) | {"reasoning_effort"},
        strip_cache_control=False,
        sdk=True,
    )


_AZURE_PARAMS = frozenset(
    {
        "temperature",
        "stream_options",
        "stop",
        "max_tokens",
        "max_completion_tokens",
        "tools",
        "tool_choice",
        "user",
        "top_p",
        "response_format",
        "parallel_tool_calls",
        "web_search_options",
        "prompt_cache_key",
    }
)


def _azure_map(params: Params, model: str) -> Params:
    renames = any(m in model for m in ("gpt-5", "gpt-6")) or "gpt5_series" in model
    out: Params = {}
    for key, value in params.items():
        if key == "max_tokens" and renames:
            out.setdefault("max_completion_tokens", value)
        else:
            out[key] = value
    return out


def _azure_provider(model: str) -> ChatProvider:
    if "o1" in model or "o3" in model or "o4" in model or "o_series/" in model:
        return ChatProvider(
            supported=lambda m: _o_series_supported(m, "azure"),
            map_params=_o_series_map,
            transform=lambda messages, _p, m: _system_as_user(messages, m),
            sdk=True,
        )
    if is_gpt5_model(model) or "gpt5_series" in model:
        return ChatProvider(
            supported=lambda m: _gpt5_supported(m, f"azure/{m}") | {"tool_choice"},
            map_params=_azure_gpt5_map,
            strip_cache_control=False,
            sdk=True,
        )
    return ChatProvider(
        supported=lambda _m: set(_AZURE_PARAMS),
        map_params=_azure_map,
        strip_cache_control=False,
        sdk=True,
    )


def _drop_internal_fields(
    fields: tuple[str, ...],
) -> Callable[[Messages, Params, str], Messages]:
    def transform(messages: Messages, _params: Params, _model: str) -> Messages:
        return [
            {k: v for k, v in m.items() if k not in fields}
            if m.get("role") == "assistant"
            else m
            for m in messages
        ]

    return transform


_TOGETHER_ADJUSTABLE = frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"})
_TOGETHER_HYBRID = frozenset(
    {
        "MiniMaxAI/MiniMax-M3",
        "Qwen/Qwen3.5-9B",
        "Qwen/Qwen3.6-Plus",
        "deepseek-ai/DeepSeek-V4-Pro",
        "moonshotai/Kimi-K3",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "zai-org/GLM-5.2",
    }
)
_TOGETHER_HIGH_MAX_PREFIX = "deepseek-ai/DeepSeek-V4-Pro"
_TOGETHER_EFFORT = {"minimal": "low", "xhigh": "high", "max": "high"}
_TOGETHER_HIGH_MAX_EFFORT = {
    "minimal": "high",
    "low": "high",
    "medium": "high",
    "xhigh": "max",
}


def _together_reasoning(model: str) -> bool:
    if model in _TOGETHER_ADJUSTABLE or model in _TOGETHER_HYBRID:
        return True
    return model.startswith(_TOGETHER_HIGH_MAX_PREFIX) or supports_reasoning(
        model, "together_ai"
    )


def _together_flag_false(model: str, flag: str) -> bool:
    if caps.supports(model, "together_ai", flag):
        return False
    entry = model_cost().get(f"together_ai/{model}")
    return isinstance(entry, dict) and entry.get(flag) is False


def _together_map(params: Params, model: str) -> Params:
    out = dict(params)
    if _together_flag_false(model, "supports_function_calling"):
        for key in ("tools", "tool_choice"):
            out.pop(key, None)
    if "response_format" in out and _together_flag_false(
        model, "supports_response_schema"
    ):
        out.pop("response_format")
    effort = out.pop("reasoning_effort", None)
    if isinstance(effort, str):
        for key, value in _together_effort_payload(effort, model).items():
            out.setdefault(key, value)
    elif effort is not None:
        out["reasoning_effort"] = effort
    return out


def _together_effort_payload(effort: str, model: str) -> Params:
    if effort == "default":
        return {}
    if model in _TOGETHER_ADJUSTABLE:
        return {
            "reasoning_effort": "low"
            if effort == "none"
            else _TOGETHER_EFFORT.get(effort, effort)
        }
    if effort == "none":
        return {"reasoning": {"enabled": False}}
    info = caps.model_info(model, "together_ai")
    declared = caps.declared_efforts(info) if info else None
    if effort in (declared or ()):
        return {"reasoning_effort": effort}
    if model.startswith(_TOGETHER_HIGH_MAX_PREFIX):
        return {"reasoning_effort": _TOGETHER_HIGH_MAX_EFFORT.get(effort, effort)}
    return {"reasoning_effort": _TOGETHER_EFFORT.get(effort, effort)}


def _groq_compound(model: str) -> bool:
    return "compound" in model


def _groq_supported(model: str) -> set[str]:
    supported = gpt_params(model)
    if not (
        _groq_compound(model) or caps.supports(model, "groq", "supports_web_search")
    ):
        supported.discard("web_search_options")
    if supports_reasoning(model, "groq"):
        supported.add("reasoning_effort")
    return supported


def _groq_map(params: Params, model: str) -> Params:
    out = dict(params)
    web_search = out.pop("web_search_options", None)
    if web_search is None or _groq_compound(model):
        return out
    tools = out.get("tools") or []
    if not any(t.get("type") == "browser_search" for t in tools):
        out["tools"] = [*tools, {"type": "browser_search"}]
    return out


def _groq_transform(messages: Messages, _params: Params, _model: str) -> Messages:
    return [
        {k: v for k, v in m.items() if v is not None}
        if m.get("role") == "assistant"
        else m
        for m in messages
    ]


def resolve_fireworks_model(model: str) -> str:
    stripped = model.removeprefix("fireworks_ai/")
    if stripped.startswith(("accounts/", "azureml://")) or "#" in stripped:
        return stripped
    if stripped.startswith(("routers/", "models/")):
        return f"accounts/fireworks/{stripped}"
    if stripped.endswith("-fast"):
        return f"accounts/fireworks/routers/{stripped}"
    return f"accounts/fireworks/models/{stripped}"


def _fireworks_flag(model: str, flag: str) -> bool | None:
    short = model.split("/")[-1]
    for key in (
        model,
        f"fireworks_ai/{short}",
        f"fireworks_ai/accounts/fireworks/models/{short}",
    ):
        info = model_cost().get(key)
        if info is not None and info.get(flag) is not None:
            return bool(info[flag])
    return None


def _fireworks_supported(model: str) -> set[str]:
    supported = {
        "max_completion_tokens",
        "max_tokens",
        "temperature",
        "top_p",
        "stop",
        "response_format",
        "user",
        "prompt_cache_key",
        "stream_options",
    }
    # litellm's fireworks provider info defaults function calling to on.
    if _fireworks_flag(model, "supports_function_calling") is not False:
        supported |= {"tools", "parallel_tool_calls"}
    if _fireworks_flag(model, "supports_tool_choice") or caps.supports(
        model, "fireworks_ai", "supports_tool_choice"
    ):
        supported.add("tool_choice")
    if _fireworks_flag(model, "supports_reasoning") or supports_reasoning(
        model, "fireworks_ai"
    ):
        supported.add("reasoning_effort")
    return supported


def _fireworks_map(params: Params, model: str) -> Params:
    out: Params = {}
    for key, value in params.items():
        if key == "tool_choice":
            out["tool_choice"] = "any" if value == "required" else value
        elif key == "max_completion_tokens":
            out["max_tokens"] = value
        elif key == "reasoning_effort":
            if value is True:
                out["reasoning_effort"] = "medium"
            elif value is False:
                out["reasoning_effort"] = "none"
            elif value != "auto":
                out["reasoning_effort"] = value
        elif value is not None:
            out[key] = value
    return out


def _fireworks_transform(messages: Messages, params: Params, _model: str) -> Messages:
    tools = params.get("tools")
    if tools:
        params["tools"] = [
            {**t, "function": {k: v for k, v in t["function"].items() if k != "strict"}}
            if t.get("type") == "function"
            else t
            for t in tools
        ]
    return [
        {
            k: v
            for k, v in m.items()
            if k
            not in ("provider_specific_fields", "thinking_blocks", "reasoning_content")
        }
        for m in messages
    ]


def _xai_supported(model: str) -> set[str]:
    supported = {
        "max_tokens",
        "parallel_tool_calls",
        "response_format",
        "stream_options",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
        "user",
        "web_search_options",
    }
    if not any(m in model for m in ("grok-3-mini", "grok-4", "grok-code-fast")):
        supported.add("stop")
    if supports_reasoning(model, "xai"):
        supported.add("reasoning_effort")
    return supported


def _xai_map(params: Params, model: str) -> Params:
    out: Params = {}
    for key, value in params.items():
        if key == "max_completion_tokens":
            out["max_tokens"] = value
        elif key == "tools" and value is not None:
            tools = [_strip_key(tool, "strict") for tool in value]
            if tools:
                out["tools"] = tools
        elif value is not None:
            out[key] = value
    return out


def _strip_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return {k: _strip_key(v, key) for k, v in value.items() if k != key}
    if isinstance(value, list):
        return [_strip_key(v, key) for v in value]
    return value


def _xai_transform(messages: Messages, _params: Params, _model: str) -> Messages:
    return [{k: v for k, v in m.items() if k != "name"} for m in messages]


def _xai_chunk(chunk: dict) -> dict:
    """litellm ``_fold_reasoning_tokens_into_completion``.

    xAI keeps reasoning out of ``completion_tokens`` but inside
    ``total_tokens``; fold it in when the totals prove that (idempotent).
    """
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return chunk
    details = usage.get("completion_tokens_details")
    reasoning = parse_token_count(
        details.get("reasoning_tokens") if isinstance(details, dict) else None
    )
    prompt = parse_token_count(usage.get("prompt_tokens"))
    completion = parse_token_count(usage.get("completion_tokens"))
    total = parse_token_count(usage.get("total_tokens"))
    if reasoning > 0 and total == prompt + completion + reasoning:
        return {
            **chunk,
            "usage": {**usage, "completion_tokens": completion + reasoning},
        }
    return chunk


def _cerebras_supported(model: str) -> set[str]:
    supported = {
        "max_tokens",
        "max_completion_tokens",
        "response_format",
        "stop",
        "temperature",
        "top_p",
        "tool_choice",
        "tools",
        "user",
    }
    if supports_reasoning(model, "cerebras"):
        supported.add("reasoning_effort")
    return supported


def _nvidia_supported(model: str) -> set[str]:
    if model in (
        "google/recurrentgemma-2b",
        "google/gemma-2-27b-it",
        "google/gemma-2-9b-it",
        "gemma-2-9b-it",
    ):
        return {"temperature", "top_p", "max_tokens", "stop"}
    if model == "nvidia/nemotron-4-340b-instruct":
        return {"temperature", "top_p", "max_tokens", "max_completion_tokens"}
    if model == "nvidia/nemotron-4-340b-reward":
        return set()
    if model == "google/codegemma-1.1-7b":
        return {"temperature", "top_p", "max_tokens", "max_completion_tokens", "stop"}
    return {
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
    }


def _perplexity_supported(model: str) -> set[str]:
    supported = {
        "max_tokens",
        "max_completion_tokens",
        "response_format",
        "temperature",
        "top_p",
    }
    if supports_reasoning(model, "perplexity"):
        supported.add("reasoning_effort")
    if caps.supports(model, "perplexity", "supports_web_search"):
        supported.add("web_search_options")
    return supported


_PROVIDERS: dict[str, Callable[[str], ChatProvider]] = {
    "openai": _openai_provider,
    "azure": _azure_provider,
    "together_ai": lambda _m: ChatProvider(
        supported=lambda m: gpt_params(m)
        | ({"reasoning_effort"} if _together_reasoning(m) else set()),
        map_params=_together_map,
        transform=_drop_internal_fields(
            ("thinking_blocks", "provider_specific_fields")
        ),
    ),
    "groq": lambda _m: ChatProvider(
        supported=_groq_supported, map_params=_groq_map, transform=_groq_transform
    ),
    "fireworks_ai": lambda _m: ChatProvider(
        supported=_fireworks_supported,
        map_params=_fireworks_map,
        transform=_fireworks_transform,
    ),
    "xai": lambda _m: ChatProvider(
        supported=_xai_supported, map_params=_xai_map, transform=_xai_transform
    ),
    "perplexity": lambda _m: ChatProvider(supported=_perplexity_supported, sdk=True),
    "cerebras": lambda _m: ChatProvider(
        supported=_cerebras_supported, map_params=max_tokens_rename, sdk=True
    ),
    "nvidia_nim": lambda _m: ChatProvider(
        supported=_nvidia_supported, map_params=max_tokens_rename, sdk=True
    ),
    **EXTRA_PROVIDERS,
}


def _generic_provider(_model: str) -> ChatProvider:
    return ChatProvider(supported=gpt_params, sdk=True)


def chat_provider(provider: str, model: str) -> ChatProvider:
    return _PROVIDERS.get(provider, _generic_provider)(model)


_CHUNK_HOOKS: dict[str, Callable[[dict], dict]] = {**CHUNK_HOOKS, "xai": _xai_chunk}


def chunk_hook(provider: str) -> Callable[[dict], dict] | None:
    return _CHUNK_HOOKS.get(provider)
