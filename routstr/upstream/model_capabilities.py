"""Model capability lookups against the vendored litellm model map.

Mirrors litellm 1.101.2's ``_get_model_info_helper`` key resolution closely
enough for the provider parameter rules in ``messages_dialects``: provider-
prefixed key first, then the bare name, each accepted only when its
``litellm_provider`` matches.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from ..payment.model_cost import model_cost, openai_chat_models


@lru_cache(maxsize=1)
def _lowered_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for key in model_cost():
        keys.setdefault(key.lower(), key)
    return keys


def _entry(name: str) -> dict[str, Any] | None:
    costs = model_cost()
    if name in costs:
        return costs[name]
    key = _lowered_keys().get(name.lower())
    return costs[key] if key is not None else None


def _provider_matches(info: dict, provider: str) -> bool:
    owner = info.get("litellm_provider")
    if owner is None or owner == provider:
        return True
    return provider == "fireworks_ai" and str(owner).startswith("fireworks_ai")


def infer_provider(model: str) -> tuple[str, str] | None:
    """litellm ``get_llm_provider`` for a bare name: only OpenAI names resolve."""
    if "/" in model:
        prefix, rest = model.split("/", 1)
        if prefix in {"openai", "azure", "anthropic", "deepseek", "xai", "groq"}:
            return rest, prefix
    if model in openai_chat_models():
        return model, "openai"
    info = _entry(model)
    if info is not None and info.get("litellm_provider") in {"anthropic", "deepseek"}:
        return model, info["litellm_provider"]
    return None


def model_info(model: str, provider: str) -> dict[str, Any] | None:
    if model.startswith(f"{provider}/"):
        split = model.split("/", 1)[1]
        candidates: tuple[str, ...] = (model, model, split)
    else:
        candidates = (f"{provider}/{model}", model, model)
    for name in candidates:
        info = _entry(name)
        if info is not None and _provider_matches(info, provider):
            return {**info, "key": _lowered_keys().get(name.lower(), name)}
    # litellm's capability generalization rules apply only to unmapped names.
    if any(_entry(name) is not None for name in candidates):
        return None
    if any(_CLAUDE_FAMILY.search(name) for name in candidates):
        return {**_CLAUDE_BASELINE, "litellm_provider": provider}
    return None


_CLAUDE_FAMILY = re.compile(r"claude-[a-z]+-\d+(?:[-.]\d+)?(?:-\d{8})?")
_CLAUDE_BASELINE: dict[str, Any] = {
    "mode": "chat",
    "supports_function_calling": True,
    "supports_parallel_function_calling": True,
    "supports_vision": True,
    "supports_tool_choice": True,
    "supports_prompt_caching": True,
    "supports_response_schema": True,
    "supports_reasoning": True,
    "supports_system_messages": True,
}


def supports(model: str, provider: str | None, key: str) -> bool:
    """``litellm.utils._supports_factory``: explicit True wins, bare entry backs up."""
    if provider is None:
        resolved = infer_provider(model)
        if resolved is None:
            return False
        model, provider = resolved
    elif model.startswith(f"{provider}/"):
        model = model.split("/", 1)[1]
    info = model_info(model, provider)
    if info is None:
        return False
    value = info.get(key)
    if value is True:
        return True
    if value is None:
        bare = _entry(model)
        return bool(bare and bare.get(key) is True)
    return False


def explicitly_disabled(model: str, provider: str | None, key: str) -> bool:
    if provider is None:
        resolved = infer_provider(model)
        if resolved is None:
            return False
        model, provider = resolved
    info = model_info(model, provider) or {}
    return info.get(key) is False


def declared(model: str, provider: str | None, key: str) -> Any:
    if provider is None:
        resolved = infer_provider(model)
        if resolved is None:
            return None
        model, provider = resolved
    info = model_info(model, provider) or {}
    if info.get(key) is not None:
        return info[key]
    bare = _entry(model)
    return bare.get(key) if bare else None


_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "default")
_EFFORT_FLAGS = (
    ("none", "supports_none_reasoning_effort"),
    ("minimal", "supports_minimal_reasoning_effort"),
    ("low", "supports_low_reasoning_effort"),
    ("xhigh", "supports_xhigh_reasoning_effort"),
    ("max", "supports_max_reasoning_effort"),
)


def _bare_twin(info: dict[str, Any]) -> dict[str, Any]:
    key, provider = info.get("key"), info.get("litellm_provider")
    if not isinstance(key, str) or not isinstance(provider, str):
        return {}
    if not key.startswith(f"{provider}/"):
        return {}
    return model_cost().get(key.removeprefix(f"{provider}/")) or {}


def declared_efforts(info: dict[str, Any]) -> tuple[str, ...] | None:
    raw = info.get("reasoning_effort_levels")
    if raw is None:
        raw = _bare_twin(info).get("reasoning_effort_levels")
    if not isinstance(raw, list):
        return None
    return tuple(e for e in _EFFORT_ORDER if e in raw)


def supported_efforts(info: dict[str, Any] | None) -> tuple[str, ...] | None:
    """litellm ``resolve_supported_reasoning_efforts`` for a mapped deployment."""
    if info is None:
        return ()
    if info.get("supports_reasoning") is False:
        return ()
    bare = _bare_twin(info)
    flags = {
        effort: info[flag] if info.get(flag) is not None else bare.get(flag)
        for effort, flag in _EFFORT_FLAGS
    }
    has_flag = any(v is not None for v in flags.values())
    if info.get("supports_reasoning") is not True and not has_flag:
        return ()
    declared_levels = declared_efforts(info)
    if declared_levels is not None:
        return declared_levels
    if not has_flag:
        return None
    allowed = {"medium", "high"}
    allowed |= {e for e in ("minimal", "low") if flags[e] is not False}
    allowed |= {e for e in ("xhigh", "max") if flags[e] is True}
    if flags["none"] is not False:
        allowed.add("none")
    return tuple(e for e in _EFFORT_ORDER if e in allowed)
