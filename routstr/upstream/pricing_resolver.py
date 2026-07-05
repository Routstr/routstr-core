"""Shared price/metadata resolution chain for upstream model discovery.

Most OpenAI-compatible ``/models`` responses carry no pricing. Rather than let
a provider fabricate one, this module resolves a model through decreasingly
trustworthy sources — litellm's bundled cost map (curated list prices, mirrors
provider docs), then the OpenRouter feed (resale prices, broader coverage) —
and returns ``None`` when none of them know the model, so the caller can fail
closed instead of inventing a number.

Provider-native pricing (a gateway's own ``/models`` schema, e.g. Venice's
``model_spec``) is authoritative and handled by the provider before this chain
is consulted; only the shared fallback lives here so a later refactor can hoist
it into the base provider unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ResolvedPricing:
    """Per-token pricing plus whatever metadata the answering source carried.

    Prices are USD per token. ``source`` records provenance
    (``native``/``litellm``/``openrouter``/``unresolved``) so later work can
    surface where each price came from.
    """

    prompt: float
    completion: float
    context_length: int | None
    source: str
    modality: str | None = None
    max_completion_tokens: int | None = None
    input_cache_read: float = 0.0
    input_cache_write: float = 0.0
    input_modalities: list[str] = field(default_factory=lambda: ["text"])
    output_modalities: list[str] = field(default_factory=lambda: ["text"])
    tokenizer: str = "unknown"
    instruct_type: str | None = None
    is_moderated: bool | None = None


def estimate_context_length(model_id: str) -> int:
    """Best-effort context window from a model id when no source reports one.

    The last rung of the fallback chain, reached only for a model whose price
    resolved but whose context did not (or that imported disabled). Context is
    not a billing input, so a rough id-based guess is acceptable here where a
    guessed *price* never would be.
    """
    lowered = model_id.lower()
    if any(pattern in lowered for pattern in ["32k", "32000"]):
        return 32768
    if any(pattern in lowered for pattern in ["16k", "16000"]):
        return 16384
    if any(pattern in lowered for pattern in ["8k", "8000"]):
        return 8192
    if "gpt-4" in lowered:
        return 8192
    if "claude" in lowered:
        return 200000
    return 4096


def _as_float(value: object) -> float | None:
    """OpenRouter reports prices as strings; coerce, ``None`` if unparseable."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    """Coerce an already-numeric token count to ``int``, else ``None``."""
    return int(value) if isinstance(value, (int, float)) else None


def _from_litellm(model_id: str) -> ResolvedPricing | None:
    # Lazy import so the resolver stays import-light and shares the exact
    # lookup semantics used by cache-rate backfill.
    from ..payment.models import litellm_cost_entry

    info = litellm_cost_entry(model_id)
    if info is None:
        return None

    prompt = info.get("input_cost_per_token")
    completion = info.get("output_cost_per_token")
    if not isinstance(prompt, (int, float)) or not isinstance(completion, (int, float)):
        return None
    # A both-zero entry is litellm listing a model without a real price (free
    # moderation/rerank tiers do this) — treating 0/0 as resolved would serve
    # the model for free. Reject it (and any negative) so the caller falls
    # through, mirroring async_fetch_openrouter_models' _has_valid_pricing.
    if prompt < 0 or completion < 0 or (prompt == 0 and completion == 0):
        return None

    input_modalities = ["text"]
    if info.get("supports_vision"):
        input_modalities.append("image")

    return ResolvedPricing(
        prompt=float(prompt),
        completion=float(completion),
        # max_input_tokens is the context window; max_tokens is litellm's
        # completion cap (it tracks max_output_tokens for ~94% of models), so
        # it is never a context source. A missing window falls to the id-based
        # estimate downstream rather than borrowing the output cap.
        context_length=_as_int(info.get("max_input_tokens")),
        source="litellm",
        max_completion_tokens=_as_int(info.get("max_output_tokens")),
        input_cache_read=float(info.get("cache_read_input_token_cost") or 0.0),
        input_cache_write=float(info.get("cache_creation_input_token_cost") or 0.0),
        input_modalities=input_modalities,
    )


def _match_openrouter(model_id: str, feed: list[dict]) -> dict | None:
    """Find ``model_id`` in the OpenRouter feed, exact id before bare tail.

    Bare-tail matching (``deepseek-chat`` ↔ ``deepseek/deepseek-chat``) is a
    looser, lower-trust match — OpenRouter fans a model out across resellers —
    so an exact id match always wins first. When several entries share the bare
    tail, the highest-priced one wins: the choice must be deterministic (not
    feed-order-dependent) and money-safe, since undercharging is the hazard.
    The live feed has no such collisions today; this only governs the latent
    case.
    """
    bare = model_id.split("/", 1)[-1]
    exact = next((m for m in feed if m.get("id") == model_id), None)
    if exact is not None:
        return exact
    matches = [m for m in feed if m.get("id", "").split("/", 1)[-1] == bare]
    if not matches:
        return None
    return max(
        matches,
        key=lambda m: _as_float(m.get("pricing", {}).get("prompt")) or 0.0,
    )


def _from_openrouter(model_id: str, feed: list[dict]) -> ResolvedPricing | None:
    entry = _match_openrouter(model_id, feed)
    if entry is None:
        return None

    pricing = entry.get("pricing", {})
    prompt = _as_float(pricing.get("prompt"))
    completion = _as_float(pricing.get("completion"))
    if prompt is None or completion is None:
        return None

    architecture = entry.get("architecture", {})
    top_provider = entry.get("top_provider", {})

    return ResolvedPricing(
        prompt=prompt,
        completion=completion,
        context_length=_as_int(entry.get("context_length")),
        source="openrouter",
        modality=architecture.get("modality"),
        max_completion_tokens=_as_int(top_provider.get("max_completion_tokens")),
        input_cache_read=_as_float(pricing.get("input_cache_read")) or 0.0,
        input_cache_write=_as_float(pricing.get("input_cache_write")) or 0.0,
        input_modalities=architecture.get("input_modalities") or ["text"],
        output_modalities=architecture.get("output_modalities") or ["text"],
        tokenizer=architecture.get("tokenizer") or "unknown",
        instruct_type=architecture.get("instruct_type"),
        is_moderated=top_provider.get("is_moderated"),
    )


class FallbackPricingResolver:
    """Resolves models via litellm → OpenRouter for one discovery pass.

    The OpenRouter catalog is fetched at most once and only when a model
    actually misses litellm, so a provider full of litellm-known models never
    touches the network. Instantiate one per ``fetch_models`` call.
    """

    def __init__(self) -> None:
        self._openrouter_feed: list[dict] | None = None

    async def resolve(self, model_id: str) -> ResolvedPricing | None:
        """Resolve ``model_id``; ``None`` if no source knows it."""
        resolved = _from_litellm(model_id)
        if resolved is not None:
            return resolved

        if self._openrouter_feed is None:
            # Lazy import so tests can patch the feed at its source.
            from ..payment.models import async_fetch_openrouter_models

            self._openrouter_feed = await async_fetch_openrouter_models()
        return _from_openrouter(model_id, self._openrouter_feed)
