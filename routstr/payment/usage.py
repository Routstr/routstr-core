"""Vendor-agnostic normalization of upstream usage objects.

Upstream providers report token usage in vendor dialects that differ in field
names and in whether cached tokens are included in the input count:

* OpenAI / Azure / xAI / Groq / Moonshot / Qwen / Gemini-compat: cache reads in
  ``prompt_tokens_details.cached_tokens``, included in ``prompt_tokens``.
* OpenAI Responses: cache reads in ``input_tokens_details.cached_tokens``,
  included in ``input_tokens``.
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
the vendor's input shape, not the cache field alone:

* ``prompt_tokens`` present -> cached + cache-write tokens are *included* in it
  (OpenAI family, DeepSeek, OpenRouter, litellm); subtract both so
  ``input_tokens`` holds only the regular-rate portion.
* only ``input_tokens`` (Anthropic native) -> cached tokens are *additive*;
  leave ``input_tokens`` untouched.
* ``input_tokens_details`` present (OpenAI Responses) -> cached tokens are
  included in ``input_tokens``. Only ``ledger_usage`` splits them out: billing
  still charges them at the input rate, and changing that is a pricing decision.

``normalize_usage`` maps all of them onto one canonical ``NormalizedUsage``
shape so billing code needs no vendor knowledge. The known dialects' field
names do not collide, so a single union parser is safe; a vendor whose fields
would genuinely conflict needs a dedicated branch here.
"""

import math
from dataclasses import dataclass
from typing import TypedDict

from pydantic.v1 import BaseModel


class UsageObservedFields(TypedDict):
    input_observed: bool
    output_observed: bool
    cache_read_observed: bool
    cache_creation_observed: bool


class UsageSources(TypedDict):
    input_source: str
    output_source: str
    cache_read_source: str
    cache_creation_source: str


@dataclass(frozen=True)
class UsageFieldPresence:
    """Whether each canonical usage dimension was parseably reported."""

    input_observed: bool = False
    output_observed: bool = False
    cache_read_observed: bool = False
    cache_creation_observed: bool = False
    input_estimated: bool = False
    output_estimated: bool = False
    cache_read_estimated: bool = False
    cache_creation_estimated: bool = False

    def sources_dict(self) -> UsageSources:
        def source(name: str) -> str:
            if getattr(self, name + "_observed"):
                return "reported"
            return "estimated" if getattr(self, name + "_estimated") else "missing"

        return {
            "input_source": source("input"),
            "output_source": source("output"),
            "cache_read_source": source("cache_read"),
            "cache_creation_source": source("cache_creation"),
        }

    def merged(self, other: "UsageFieldPresence") -> "UsageFieldPresence":
        return UsageFieldPresence(
            input_estimated=self.input_estimated or other.input_estimated,
            output_estimated=self.output_estimated or other.output_estimated,
            cache_read_estimated=self.cache_read_estimated
            or other.cache_read_estimated,
            cache_creation_estimated=self.cache_creation_estimated
            or other.cache_creation_estimated,
            input_observed=self.input_observed or other.input_observed,
            output_observed=self.output_observed or other.output_observed,
            cache_read_observed=(self.cache_read_observed or other.cache_read_observed),
            cache_creation_observed=(
                self.cache_creation_observed or other.cache_creation_observed
            ),
        )

    def as_dict(self) -> UsageObservedFields:
        return {
            "input_observed": self.input_observed,
            "output_observed": self.output_observed,
            "cache_read_observed": self.cache_read_observed,
            "cache_creation_observed": self.cache_creation_observed,
        }


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


def _is_parseable_token_count(value: object) -> bool:
    """Return whether a value is a usable non-negative token count."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float):
        return math.isfinite(value) and value >= 0
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return False
        return math.isfinite(parsed) and parsed >= 0
    return False


def _has_parseable_field(data: object, *fields: str) -> bool:
    if not isinstance(data, dict):
        return False
    return any(
        field in data and _is_parseable_token_count(data[field]) for field in fields
    )


def usage_field_presence(usage_data: object) -> UsageFieldPresence:
    """Capture raw usage-field presence before numeric normalization.

    Locally estimated usage is intentionally unobserved. Explicit zero is
    observed, while an absent or unusable value is not.
    """
    if not isinstance(usage_data, dict):
        return UsageFieldPresence()

    prompt_details = usage_data.get("prompt_tokens_details")
    input_details = usage_data.get("input_tokens_details")
    cache_read_observed = (
        _has_parseable_field(usage_data, "cache_read_input_tokens")
        or _has_parseable_field(prompt_details, "cached_tokens")
        or _has_parseable_field(input_details, "cached_tokens")
        or _has_parseable_field(usage_data, "prompt_cache_hit_tokens")
    )
    cache_creation_observed = (
        _has_parseable_field(usage_data, "cache_creation_input_tokens")
        or _has_parseable_field(
            prompt_details, "cache_creation_tokens", "cache_write_tokens"
        )
        or _has_parseable_field(
            input_details, "cache_creation_tokens", "cache_write_tokens"
        )
    )
    presence = UsageFieldPresence(
        input_observed=_has_parseable_field(
            usage_data, "prompt_tokens", "input_tokens"
        ),
        output_observed=_has_parseable_field(
            usage_data, "completion_tokens", "output_tokens"
        ),
        cache_read_observed=cache_read_observed,
        cache_creation_observed=cache_creation_observed,
    )
    if usage_data.get("estimated") is True:
        return UsageFieldPresence(
            input_estimated=presence.input_observed,
            output_estimated=presence.output_observed,
            cache_read_estimated=presence.cache_read_observed,
            cache_creation_estimated=presence.cache_creation_observed,
        )
    return presence


def _first_token_count(usage_data: dict, *fields: str) -> int:
    """Return the first positive token count among the given fields."""
    for field in fields:
        value = parse_token_count(usage_data.get(field, 0))
        if value > 0:
            return value
    return 0


def _extract_cache_tokens(
    usage_data: dict, responses_details: bool = False
) -> tuple[int, int]:
    """Pull (cache_read, cache_write) across all known dialects.

    Precedence (highest first), independent for reads and writes:

    * Anthropic top-level: ``cache_read_input_tokens`` /
      ``cache_creation_input_tokens``.
    * Nested ``prompt_tokens_details`` (and ``input_tokens_details`` when
      ``responses_details`` is set): ``cached_tokens`` for reads;
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

    input_details = usage_data.get("input_tokens_details")
    if responses_details and isinstance(input_details, dict):
        if not cache_read:
            cache_read = parse_token_count(input_details.get("cached_tokens", 0))
        if not cache_write:
            cache_write = _first_token_count(
                input_details, "cache_creation_tokens", "cache_write_tokens"
            )

    if not cache_read:
        # DeepSeek: prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens
        cache_read = parse_token_count(usage_data.get("prompt_cache_hit_tokens", 0))

    return cache_read, cache_write


def normalize_usage(
    usage_data: object, *, responses_details: bool = False
) -> NormalizedUsage | None:
    """Map a vendor usage dict onto the canonical shape, or None if absent.

    Cached reads and writes are subtracted from the input count exactly once,
    only for dialects that report a ``prompt_tokens`` grand total that already
    includes them (OpenAI family, DeepSeek, OpenRouter, litellm). Anthropic
    native reports them additively under ``input_tokens`` and is left untouched.
    ``responses_details`` also splits Responses-style ``input_tokens_details``.
    """
    if not isinstance(usage_data, dict):
        return None

    output_tokens = _first_token_count(
        usage_data, "completion_tokens", "output_tokens"
    )
    cache_read, cache_write = _extract_cache_tokens(usage_data, responses_details)

    # ``prompt_tokens`` is the inclusive grand total; ``input_tokens`` (Anthropic
    # native) excludes cached tokens. The field chosen decides whether to subtract.
    if "prompt_tokens" in usage_data:
        input_tokens = parse_token_count(usage_data.get("prompt_tokens", 0))
        input_tokens = max(0, input_tokens - cache_read - cache_write)
    else:
        input_tokens = parse_token_count(usage_data.get("input_tokens", 0))
        if responses_details and isinstance(
            usage_data.get("input_tokens_details"), dict
        ):
            input_tokens = max(0, input_tokens - cache_read - cache_write)

    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def ledger_usage(usage_data: object) -> NormalizedUsage:
    """Usage for stats records, with the Responses cache split billing omits."""
    return normalize_usage(usage_data, responses_details=True) or NormalizedUsage()
