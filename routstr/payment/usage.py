"""Vendor-agnostic normalization of upstream usage objects.

Upstream providers report token usage in vendor dialects that differ in field
names and in whether cached tokens are included in the input count:

* OpenAI: ``prompt_tokens_details.cached_tokens``, included in ``prompt_tokens``
* Anthropic: ``cache_read_input_tokens`` / ``cache_creation_input_tokens``,
  additive to (not included in) ``input_tokens``
* DeepSeek: ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``, with
  ``prompt_tokens = hit + miss``

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


def normalize_usage(usage_data: object) -> NormalizedUsage | None:
    """Map a vendor usage dict onto the canonical shape, or None if absent.

    Cached tokens are subtracted from the input count exactly once, only for
    dialects that include them in it (OpenAI, DeepSeek). Precedence between
    cache fields: Anthropic explicit > OpenAI details > DeepSeek hit/miss.
    """
    if not isinstance(usage_data, dict):
        return None

    input_tokens = _first_token_count(usage_data, "prompt_tokens", "input_tokens")
    output_tokens = _first_token_count(
        usage_data, "completion_tokens", "output_tokens"
    )
    cache_write = parse_token_count(usage_data.get("cache_creation_input_tokens", 0))

    # Anthropic: cache reads are additive, input_tokens stays untouched
    cache_read = parse_token_count(usage_data.get("cache_read_input_tokens", 0))

    if not cache_read:
        # OpenAI: cached tokens are included in prompt_tokens
        prompt_details = usage_data.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cache_read = parse_token_count(prompt_details.get("cached_tokens", 0))
        # DeepSeek: prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens
        if not cache_read:
            cache_read = parse_token_count(
                usage_data.get("prompt_cache_hit_tokens", 0)
            )
        if cache_read:
            input_tokens = max(0, input_tokens - cache_read)

    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
