"""Inject explicit prompt-cache breakpoints into OpenAI-shaped requests.

Some upstreams cache *explicitly*: the request must carry
``cache_control: {"type": "ephemeral"}`` markers on the content blocks that
should be cached. Two model families use this identical wire format:

* **Anthropic Claude** — direct or via OpenRouter's ``anthropic/*`` models.
* **Alibaba's explicit-cache models on OpenRouter** — ``qwen/qwen3-max``,
  ``qwen/qwen-plus``, ``qwen/qwen3.6-plus``, ``qwen/qwen3-coder-plus``,
  ``qwen/qwen3-coder-flash`` and ``deepseek/deepseek-v3.2`` — which OpenRouter
  documents as using "the same syntax as Anthropic explicit caching".

Every other provider routstr proxies (OpenAI, Azure, xAI/Grok, Groq, Moonshot,
default DeepSeek, Gemini implicit, Fireworks) caches *automatically* and needs
no markers — they are left untouched.

A client that doesn't know it is talking to one of these models *through*
routstr (e.g. an OpenAI-compatible coding agent pointed at a routstr URL) never
emits the markers — it only adds them when it recognises the provider as
OpenRouter. So caching silently never engages over routstr even though the same
client caches fine talking to OpenRouter directly.

This module restores caching by stamping the standard breakpoints onto the
forwarded body — the system prompt, the last tool, and the last conversation
message (the format allows up to four; we use three, matching the common
agent convention) — but only when the client supplied none of its own, so
explicit client control always wins. The caller is responsible for only
applying this toward an upstream that accepts the markers (OpenRouter /
Anthropic), so they never leak to a provider that would reject them.
"""

from __future__ import annotations

from typing import Any

# The single ephemeral marker stamped onto each chosen breakpoint. A 5-minute
# TTL (the default for ``ephemeral``) — deliberately not the 1h tier, which
# carries a higher cache-write premium and should stay opt-in.
EPHEMERAL_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

# Alibaba's explicit-cache models on OpenRouter. Matched as substrings of the
# model id (any spelling routstr carries). Snapshot endpoints that OpenRouter
# documents as *not* supporting explicit caching (e.g. ``qwen3.5-plus-02-15``)
# are different families and deliberately absent from this list.
_ALIBABA_EXPLICIT_CACHE_SLUGS: tuple[str, ...] = (
    "qwen3-max",
    "qwen-plus",
    "qwen3.6-plus",
    "qwen3-coder-plus",
    "qwen3-coder-flash",
    "deepseek-v3.2",
)


def is_explicit_cache_model(model_id: str | None, *fallbacks: str | None) -> bool:
    """True when the target model uses the explicit ``cache_control`` dialect.

    Covers the Claude family (broadly — every Claude model supports it) and
    Alibaba's documented explicit-cache models, across the id spellings routstr
    carries: the OpenRouter id (``anthropic/claude-...``, ``qwen/qwen3-max``),
    the bare upstream id, and any forwarded/canonical alias.
    """
    for candidate in (model_id, *fallbacks):
        if not candidate:
            continue
        lowered = candidate.lower()
        if "claude" in lowered or "anthropic/" in lowered:
            return True
        if any(slug in lowered for slug in _ALIBABA_EXPLICIT_CACHE_SLUGS):
            return True
    return False


def _has_cache_control(obj: Any) -> bool:
    """Recursively detect any client-supplied ``cache_control`` marker."""
    if isinstance(obj, dict):
        if "cache_control" in obj:
            return True
        return any(_has_cache_control(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_cache_control(v) for v in obj)
    return False


def body_has_cache_control(data: dict) -> bool:
    """True when the request already carries cache_control on messages/tools."""
    return _has_cache_control(data.get("messages")) or _has_cache_control(
        data.get("tools")
    )


def _stamp_text_content(message: dict) -> bool:
    """Add the ephemeral marker to a message's last text block.

    A string content is promoted to the array form Anthropic requires for
    cache markers; an existing array gets the marker on its last text part.
    Returns True when a marker was placed.
    """
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return False
        message["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": dict(EPHEMERAL_CACHE_CONTROL),
            }
        ]
        return True
    if isinstance(content, list):
        for part in reversed(content):
            if isinstance(part, dict) and part.get("type") == "text":
                part["cache_control"] = dict(EPHEMERAL_CACHE_CONTROL)
                return True
    return False


def _stamp_system_prompt(messages: list) -> None:
    for message in messages:
        if isinstance(message, dict) and message.get("role") in (
            "system",
            "developer",
        ):
            _stamp_text_content(message)
            return


def _stamp_last_tool(tools: Any) -> None:
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1]["cache_control"] = dict(EPHEMERAL_CACHE_CONTROL)


def _stamp_last_conversation_message(messages: list) -> None:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") in ("user", "assistant"):
            if _stamp_text_content(message):
                return


def inject_anthropic_cache_breakpoints(data: dict) -> bool:
    """Stamp ephemeral cache breakpoints onto an OpenAI-shaped chat body.

    Mutates ``data`` in place (the established convention in
    ``prepare_request_body``) and returns True when anything changed. No-ops
    when the body isn't chat-shaped or the client already set cache_control.
    """
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    if body_has_cache_control(data):
        return False

    _stamp_system_prompt(messages)
    _stamp_last_tool(data.get("tools"))
    _stamp_last_conversation_message(messages)
    return True
