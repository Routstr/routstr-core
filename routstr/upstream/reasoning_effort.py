"""Map client reasoning/thinking effort onto a model's allowlist.

OpenRouter (and some other catalogs) publish per-model reasoning metadata:
which effort levels are legal, the default, and whether reasoning is
mandatory. Clients still send the generic OpenAI / Anthropic shapes
(``reasoning_effort``, ``reasoning.effort``, ``thinking``). This module
normalizes those into a supported effort and writes the fields the
upstream actually accepts, instead of dropping the parameter or
forwarding a value the model rejects.
"""

from __future__ import annotations

from typing import Any

from ..payment.models import Model, Reasoning

# Highest first. Unknown values are treated as unranked.
EFFORT_RANK: tuple[str, ...] = (
    "max",
    "xhigh",
    "high",
    "medium",
    "low",
    "minimal",
    "none",
)
_RANK_INDEX: dict[str, int] = {name: i for i, name in enumerate(EFFORT_RANK)}

_REASONING_KEYS = ("reasoning", "reasoning_effort", "thinking")


def _normalize_effort(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def closest_supported_effort(
    requested: str | None,
    supported: list[str],
    *,
    default_effort: str | None = None,
    mandatory: bool = False,
) -> str | None:
    """Pick a legal effort for ``requested``.

    Exact match wins. Otherwise the nearest rank in ``EFFORT_RANK`` is
    used (preferring the higher neighbour on a tie). ``none`` is rejected
    when ``mandatory`` is set. Missing / unmapped requests fall back to
    ``default_effort``, then the highest remaining supported level.
    """
    allowed_efforts: list[str] = [
        normalized
        for item in supported
        if (normalized := _normalize_effort(item)) is not None
    ]
    if mandatory:
        allowed_efforts = [item for item in allowed_efforts if item != "none"]
    if not allowed_efforts:
        if mandatory:
            return _normalize_effort(default_effort)
        return _normalize_effort(requested) or _normalize_effort(default_effort)

    default = _normalize_effort(default_effort)
    if default not in allowed_efforts:
        default = allowed_efforts[0]

    requested_norm = _normalize_effort(requested)
    if requested_norm is None or (requested_norm == "none" and mandatory):
        return default

    if requested_norm in allowed_efforts:
        return requested_norm

    if requested_norm not in _RANK_INDEX:
        return default

    target = _RANK_INDEX[requested_norm]
    return min(
        allowed_efforts,
        key=lambda effort: (
            abs(_RANK_INDEX.get(effort, 10_000) - target),
            _RANK_INDEX.get(effort, 10_000),
        ),
    )


def resolve_effort(requested: str | None, reasoning: Reasoning | None) -> str | None:
    """Map ``requested`` through ``reasoning`` metadata when present."""
    if reasoning is None:
        return _normalize_effort(requested)
    supported = reasoning.supported_efforts or []
    return closest_supported_effort(
        requested,
        supported,
        default_effort=reasoning.default_effort,
        mandatory=bool(reasoning.mandatory),
    )


def _effort_from_thinking(thinking: object) -> str | None:
    if not isinstance(thinking, dict):
        return None
    effort = _normalize_effort(thinking.get("effort"))
    if effort:
        return effort
    thinking_type = _normalize_effort(thinking.get("type"))
    if thinking_type in {"disabled", "none"}:
        return "none"
    return None


def extract_requested_effort(data: dict[str, Any]) -> str | None:
    """Best-effort effort string from the OpenAI / Anthropic request shapes."""
    if isinstance(data.get("reasoning"), dict):
        nested = _normalize_effort(data["reasoning"].get("effort"))
        if nested:
            return nested
    top_level = _normalize_effort(data.get("reasoning_effort"))
    if top_level:
        return top_level
    return _effort_from_thinking(data.get("thinking"))


def _request_mentions_reasoning(data: dict[str, Any]) -> bool:
    return any(key in data for key in _REASONING_KEYS)


def apply_reasoning_effort(
    data: dict[str, Any],
    model: Model,
    *,
    drop_thinking: bool = False,
) -> bool:
    """Rewrite ``data`` in place so effort matches the model allowlist.

    Returns True when ``data`` changed. Leaves the body alone when the
    caller did not send a reasoning field and the model does not require
    one. Existing ``reasoning`` object keys (``max_tokens``, ``exclude``,
    ``enabled``) are preserved; only ``effort`` is mapped.

    ``drop_thinking`` is for OpenAI-compatible backends that reject the
    Anthropic ``thinking`` object: the effort is lifted onto
    ``reasoning_effort`` / ``reasoning.effort`` and ``thinking`` is removed.
    """
    if not isinstance(data, dict):
        return False

    reasoning_meta = getattr(model, "reasoning", None)
    mentioned = _request_mentions_reasoning(data)
    if not mentioned and not (reasoning_meta and reasoning_meta.mandatory):
        return False

    resolved = resolve_effort(extract_requested_effort(data), reasoning_meta)
    changed = False

    if drop_thinking and "thinking" in data:
        data.pop("thinking", None)
        changed = True

    if resolved is None:
        return changed

    if "reasoning_effort" in data:
        if data.get("reasoning_effort") != resolved:
            data["reasoning_effort"] = resolved
            changed = True
    elif drop_thinking or (reasoning_meta and reasoning_meta.mandatory):
        # Invent the OpenAI-shaped field when we stripped Anthropic
        # ``thinking``, or when the model will reject a request with no
        # effort at all.
        if not isinstance(data.get("reasoning"), dict):
            data["reasoning_effort"] = resolved
            changed = True

    existing = data.get("reasoning")
    if isinstance(existing, dict):
        if existing.get("effort") != resolved:
            data["reasoning"] = {**existing, "effort": resolved}
            changed = True
    elif reasoning_meta and reasoning_meta.mandatory and "reasoning_effort" not in data:
        data["reasoning"] = {"effort": resolved}
        changed = True

    return changed


def adapt_messages_body_for_litellm(data: dict[str, Any], model: Model) -> None:
    """Convert Anthropic ``thinking`` into OpenAI-shaped effort for litellm.

    Litellm's Anthropic-messages adapter talking to an OpenAI-compatible
    upstream will 400 on ``thinking``. Lift the effort onto
    ``reasoning_effort`` and drop the Anthropic-only object.
    """
    apply_reasoning_effort(data, model, drop_thinking=True)
