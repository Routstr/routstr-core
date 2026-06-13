"""Vendor-agnostic normalization of upstream usage objects.

Upstream providers report token usage in vendor dialects that differ in field
names and in whether cached tokens are included in the input count:

* OpenAI / Azure / xAI / Groq / Moonshot / Qwen / Gemini-compat: cache reads in
  ``prompt_tokens_details.cached_tokens``, included in ``prompt_tokens``.
* OpenRouter: same as OpenAI plus cache *writes* in
  ``prompt_tokens_details.cache_write_tokens``, also included in
  ``prompt_tokens``.
* litellm-normalized: same nesting, but names the write field
  ``prompt_tokens_details.cache_creation_tokens`` (and additionally mirrors the
  Anthropic top-level fields), with ``prompt_tokens`` as the grand total.
* Anthropic native: ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
  top-level, additive to (not included in) ``input_tokens``.
* DeepSeek: ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``, with
  ``prompt_tokens = hit + miss``.

What decides whether cached tokens must be subtracted out of the input count is
**which prompt field the vendor uses**, not which cache field appears:

* ``prompt_tokens`` present -> cached + cache-write tokens are *included* in it
  (OpenAI family, DeepSeek, OpenRouter, litellm); subtract both so
  ``input_tokens`` holds only the regular-rate portion.
* only ``input_tokens`` (Anthropic native) -> cached tokens are *additive*;
  leave ``input_tokens`` untouched.

``normalize_usage`` maps all of them onto one canonical ``NormalizedUsage``
shape so billing code needs no vendor knowledge. The known dialects' field
names do not collide, so a single union parser is safe; vendors whose fields
would genuinely conflict override ``BaseUpstreamProvider.normalize_usage``.
"""

from pydantic.v1 import BaseModel


class NormalizedUsage(BaseModel):
    """Canonical token usage: input_tokens never includes cached tokens."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def parse_token_count(value: object) -> int:
    """Parse a token count from various formats (int, float, str, bool)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value)))
        except ValueError:
            return 0
    return 0


def _first_token_count(usage_data: dict, *fields: str) -> int:
    """Return the first positive token count among the given fields."""
    for field in fields:
        value = parse_token_count(usage_data.get(field, 0))
        if value > 0:
            return value
    return 0


def _extract_cache_tokens(usage_data: dict) -> tuple[int, int]:
    """Pull (cache_read, cache_write) across all known dialects.

    Precedence (highest first), independent for reads and writes:

    * Anthropic top-level: ``cache_read_input_tokens`` /
      ``cache_creation_input_tokens``.
    * Nested ``prompt_tokens_details``: ``cached_tokens`` for reads;
      ``cache_creation_tokens`` (litellm) or ``cache_write_tokens``
      (OpenRouter) for writes.
    * DeepSeek: ``prompt_cache_hit_tokens`` for reads (no write concept).
    """
    cache_read = parse_token_count(usage_data.get("cache_read_input_tokens", 0))
    cache_write = parse_token_count(usage_data.get("cache_creation_input_tokens", 0))

    prompt_details = usage_data.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        if not cache_read:
            cache_read = parse_token_count(prompt_details.get("cached_tokens", 0))
        if not cache_write:
            cache_write = _first_token_count(
                prompt_details, "cache_creation_tokens", "cache_write_tokens"
            )

    if not cache_read:
        # DeepSeek: prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens
        cache_read = parse_token_count(usage_data.get("prompt_cache_hit_tokens", 0))

    return cache_read, cache_write


def normalize_usage(usage_data: object) -> NormalizedUsage | None:
    """Map a vendor usage dict onto the canonical shape, or None if absent.

    Cached reads and writes are subtracted from the input count exactly once,
    only for dialects that report a ``prompt_tokens`` grand total that already
    includes them (OpenAI family, DeepSeek, OpenRouter, litellm). Anthropic
    native reports them additively under ``input_tokens`` and is left untouched.
    """
    if not isinstance(usage_data, dict):
        return None

    output_tokens = _first_token_count(
        usage_data, "completion_tokens", "output_tokens"
    )
    cache_read, cache_write = _extract_cache_tokens(usage_data)

    # ``prompt_tokens`` is the inclusive grand total; ``input_tokens`` (Anthropic
    # native) excludes cached tokens. The field chosen decides whether to subtract.
    if "prompt_tokens" in usage_data:
        input_tokens = parse_token_count(usage_data.get("prompt_tokens", 0))
        input_tokens = max(0, input_tokens - cache_read - cache_write)
    else:
        input_tokens = parse_token_count(usage_data.get("input_tokens", 0))

    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
