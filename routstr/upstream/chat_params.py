"""OpenAI chat params and model families shared by the provider rules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..payment.model_cost import openai_chat_models
from . import model_capabilities as caps

Params = dict[str, Any]
Messages = list[dict[str, Any]]

_GPT_BASE_PARAMS = frozenset(
    {
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "stream_options",
        "temperature",
        "top_p",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "web_search_options",
        "prompt_cache_key",
    }
)

_EFFORT_CHAIN = {
    "max": ("max", "xhigh", "high"),
    "xhigh": ("xhigh", "high"),
    "minimal": ("minimal", "low"),
}

# --------------------------------------------------------------------------
# OpenAI model families
# --------------------------------------------------------------------------


def is_openai_catalog_model(model: str) -> bool:
    name = model.split("responses/", 1)[1] if "responses/" in model else model
    return name in openai_chat_models()


def is_o_series_model(model: str) -> bool:
    name = model.split("/")[-1]
    return (
        len(name) > 1
        and name[0] == "o"
        and name[1].isdigit()
        and name in openai_chat_models()
    )


def is_gpt5_model(model: str) -> bool:
    name = model.split("/")[-1]
    return any(m in model for m in ("gpt-5", "gpt-6")) and not name.startswith(
        "gpt-5-chat"
    )


def gpt_params(model: str) -> set[str]:
    params = set(_GPT_BASE_PARAMS)
    if model not in ("gpt-3.5-turbo-16k", "gpt-4"):
        params.add("response_format")
    if is_openai_catalog_model(model):
        params.add("user")
    return params


def normalize_reasoning_effort(effort: str, model: str, provider: str) -> str:
    """Lower a tier the model map says the model lacks (litellm adapter)."""
    chain = _EFFORT_CHAIN.get(effort)
    if chain is None:
        return effort
    resolved = _resolve(model, provider)
    if resolved is None:
        return chain[-1]
    supported = caps.supported_efforts(caps.model_info(*resolved))
    if not supported:
        return chain[-1]
    accepted = tuple(level for level in supported if level != "none")
    return next(
        (level for level in (*chain, *accepted) if level in supported), chain[-1]
    )


def _resolve(model: str, provider: str) -> tuple[str, str] | None:
    if model.startswith(f"{provider}/"):
        model = model.split("/", 1)[1]
    return model, provider


def supports_reasoning(model: str, provider: str) -> bool:
    return caps.supports(model, provider, "supports_reasoning")


@dataclass(frozen=True)
class ChatProvider:
    """One litellm OpenAI-compatible chat config."""

    supported: Callable[[str], set[str]]
    map_params: Callable[[Params, str], Params] | None = None
    transform: Callable[[Messages, Params, str], Messages] | None = None
    strip_cache_control: bool = True
    sdk: bool = False


def max_tokens_rename(params: Params, _model: str) -> Params:
    out: Params = {}
    for key, value in params.items():
        out["max_tokens" if key == "max_completion_tokens" else key] = value
    return out
