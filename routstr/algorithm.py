"""Model prioritization algorithm for selecting cheapest upstream providers."""

from typing import TYPE_CHECKING

from .core.logging import get_logger

if TYPE_CHECKING:
    from .payment.models import Model
    from .upstream import BaseUpstreamProvider

logger = get_logger(__name__)


def calculate_model_cost_score(model: "Model") -> float:
    """Calculate a representative cost score for a model.

    This score is used to compare models when multiple providers offer the same model.
    Lower scores indicate cheaper models.

    The score is calculated as a weighted average of:
    - Input token cost (weighted by typical input usage)
    - Output token cost (weighted by typical output usage)
    - Fixed request cost

    Args:
        model: Model instance with pricing information

    Returns:
        Float representing the cost score. Lower is better.
    """
    pricing = model.pricing

    # Weight costs by typical usage patterns
    # Assume average request: 1000 input tokens, 500 output tokens
    TYPICAL_INPUT_TOKENS = 1000.0
    TYPICAL_OUTPUT_TOKENS = 500.0

    # Calculate weighted cost in USD
    input_cost = pricing.prompt * (TYPICAL_INPUT_TOKENS / 1000.0)
    output_cost = pricing.completion * (TYPICAL_OUTPUT_TOKENS / 1000.0)
    request_cost = pricing.request

    # Include additional costs if present
    image_cost = (
        getattr(pricing, "image", 0.0) * 0.1
    )  # Weight lower as not every request uses images
    web_search_cost = getattr(pricing, "web_search", 0.0) * 0.1
    reasoning_cost = getattr(pricing, "internal_reasoning", 0.0) * 0.2

    total_cost = (
        input_cost
        + output_cost
        + request_cost
        + image_cost
        + web_search_cost
        + reasoning_cost
    )

    return total_cost


def get_provider_penalty(provider: "BaseUpstreamProvider") -> float:
    """Calculate a penalty multiplier for certain providers.

    This allows applying policy-based adjustments beyond pure cost.
    For example, preferring certain providers for reliability or features.

    Args:
        provider: UpstreamProvider instance

    Returns:
        Float multiplier to apply to cost (1.0 = no penalty, >1.0 = penalize)
    """
    # Default: no penalty
    penalty = 1.0

    # Check if this is OpenRouter (can be identified by base URL)
    base_url = getattr(provider, "base_url", "")
    if "openrouter.ai" in base_url.lower():
        # Small penalty for OpenRouter to prefer other providers when costs are very close
        # This maintains the original behavior of preferring non-OpenRouter providers
        penalty = 1.001  # 0.1% penalty

    return penalty


def create_model_mappings(
    upstreams: list["BaseUpstreamProvider"],
    overrides_by_key: dict[tuple[str, int], tuple],
    disabled_model_keys: set[tuple[str, int]],
) -> tuple[
    dict[str, "Model"],
    dict[str, list[tuple["Model", "BaseUpstreamProvider"]]],
    dict[str, "Model"],
]:
    """Create optimal model mappings based on cost and provider preferences.

    This is the main entry point for the algorithm. It processes all upstream providers
    and creates three mappings based on cost optimization:

    1. model_instances: alias -> Model (all model aliases mapped to their Model objects)
    2. provider_map: alias -> List[(Model, UpstreamProvider)] (sorted candidate
       list for each alias; each provider is paired with ITS OWN model so
       failover can forward and bill the candidate that actually serves)
    3. unique_models: base_id -> Model (unique models without provider prefixes)

    The algorithm:
    - Processes non-OpenRouter providers first (they're typically cheaper)
    - Then processes OpenRouter models (they can still win if cheaper)
    - For each model alias, collects all candidates and sorts them by priority and cost.

    Args:
        upstreams: List of all upstream provider instances
        overrides_by_key: Dict of model overrides from database
            {(model_id_lower, upstream_provider_id): (ModelRow, fee)}
        disabled_model_keys: Set of provider-scoped model keys that should be excluded

    Returns:
        Tuple of (model_instances, provider_map, unique_models)
    """
    from .payment.models import _row_to_model
    from .upstream.helpers import resolve_model_alias

    candidates: dict[str, list[tuple["Model", "BaseUpstreamProvider"]]] = {}
    unique_models: dict[str, "Model"] = {}
    unique_model_keys: dict[str, str] = {}
    seen_model_provider: set[tuple[str, str]] = set()

    # Providers sharing a URL may use different credentials and expose different
    # deployments. Keep them all; candidates for the same model are ranked by
    # their fee-adjusted pricing below.
    providers_by_db_id: dict[int, "BaseUpstreamProvider"] = {}
    for upstream in upstreams:
        db_id = getattr(upstream, "db_id", None)
        if isinstance(db_id, int):
            providers_by_db_id[db_id] = upstream

    # Separate OpenRouter from other providers
    openrouter_upstreams: list["BaseUpstreamProvider"] = []
    other_upstreams: list["BaseUpstreamProvider"] = []

    for upstream in upstreams:
        base_url = getattr(upstream, "base_url", "")
        if base_url == "https://openrouter.ai/api/v1":
            openrouter_upstreams.append(upstream)
        else:
            other_upstreams.append(upstream)

    def get_base_model_id(model_id: str) -> str:
        """Get base model ID by removing provider prefix."""
        return model_id.split("/", 1)[1] if "/" in model_id else model_id

    def get_provider_identity(upstream: "BaseUpstreamProvider") -> str:
        """Get a stable provider identity used for deduplication."""
        db_id = getattr(upstream, "db_id", None)
        if isinstance(db_id, int):
            return f"db:{db_id}"

        provider_type = str(getattr(upstream, "provider_type", "") or "").lower()
        base_url = str(getattr(upstream, "base_url", "") or "").lower()
        return f"{provider_type}|{base_url}"

    def get_effective_forwarded_model_id(model: "Model") -> str | None:
        """Ignore legacy self-aliases so they keep the model's base identity."""
        forwarded_model_id = model.forwarded_model_id
        if forwarded_model_id == model.id:
            return None
        return forwarded_model_id

    def _add_candidate(
        alias: str, model: "Model", provider: "BaseUpstreamProvider"
    ) -> None:
        """Add one candidate per model/provider identity for an alias."""
        alias_lower = alias.lower()
        alias_candidates = candidates.setdefault(alias_lower, [])
        provider_identity = get_provider_identity(provider)
        if any(
            existing_model.id.lower() == model.id.lower()
            and get_provider_identity(existing_provider) == provider_identity
            for existing_model, existing_provider in alias_candidates
        ):
            return
        alias_candidates.append((model, provider))

    def record_unique_model_key(model: "Model") -> None:
        """Record one case-insensitive public ID and its display spelling."""
        base_id = get_base_model_id(model.id)
        public_id = get_effective_forwarded_model_id(model) or base_id
        unique_model_keys.setdefault(public_id.lower(), public_id)

    def process_provider_models(upstream: "BaseUpstreamProvider") -> None:
        """Process all models from a given provider."""
        upstream_prefix = getattr(upstream, "upstream_name", None)
        provider_key = get_provider_identity(upstream)
        upstream_db_id = getattr(upstream, "db_id", None)

        for model in upstream.get_cached_models():
            model_key = (
                (model.id.lower(), upstream_db_id)
                if isinstance(upstream_db_id, int)
                else None
            )
            if not model.enabled or (
                model_key is not None and model_key in disabled_model_keys
            ):
                continue

            # Apply overrides only for this provider's model row.
            if model_key is not None and model_key in overrides_by_key:
                override_row, provider_fee = overrides_by_key[model_key]
                model_to_use = _row_to_model(
                    override_row, apply_provider_fee=True, provider_fee=provider_fee
                )
            else:
                model_to_use = model

            forwarded_model_id = get_effective_forwarded_model_id(model_to_use)

            # Get all aliases for this model
            aliases = resolve_model_alias(
                model_to_use.id,
                model_to_use.canonical_slug,
                alias_ids=model_to_use.alias_ids,
            )

            # Add prefixed alias if applicable
            if upstream_prefix and "/" not in model_to_use.id:
                prefixed_id = f"{upstream_prefix}/{model_to_use.id}"
                if prefixed_id not in aliases:
                    aliases.append(prefixed_id)

            # Register a distinct forwarded_model_id as a routable alias.
            if forwarded_model_id and forwarded_model_id not in aliases:
                aliases.append(forwarded_model_id)

            # Try to set each alias
            for alias in aliases:
                _add_candidate(alias, model_to_use, upstream)
            record_unique_model_key(model_to_use)
            seen_model_provider.add((model_to_use.id.lower(), provider_key))

    # Process non-OpenRouter providers first
    for upstream in other_upstreams:
        process_provider_models(upstream)

    # Process OpenRouter last
    for upstream in openrouter_upstreams:
        process_provider_models(upstream)

    # Include enabled DB overrides even when provider discovery misses models.
    # This is important for deployment-based providers like Azure.
    for (model_id, upstream_provider_id), override_data in overrides_by_key.items():
        if (model_id, upstream_provider_id) in disabled_model_keys:
            continue
        override_row, provider_fee = override_data

        upstream_for_override = providers_by_db_id.get(upstream_provider_id)
        if upstream_for_override is None:
            continue

        provider_key = get_provider_identity(upstream_for_override)
        dedupe_key = (model_id.lower(), provider_key)
        if dedupe_key in seen_model_provider:
            continue

        try:
            model_to_use = _row_to_model(
                override_row, apply_provider_fee=True, provider_fee=provider_fee
            )
        except Exception as exc:
            logger.warning(
                "Skipping invalid model override while building model mappings",
                extra={
                    "model_id": model_id,
                    "upstream_provider_id": upstream_provider_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            continue
        if not model_to_use.enabled:
            continue

        forwarded_model_id = get_effective_forwarded_model_id(model_to_use)

        try:
            aliases = resolve_model_alias(
                model_to_use.id,
                model_to_use.canonical_slug,
                alias_ids=model_to_use.alias_ids,
            )
        except Exception as exc:
            logger.warning(
                "Skipping model aliases for invalid override model",
                extra={
                    "model_id": model_id,
                    "upstream_provider_id": upstream_provider_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            continue

        upstream_prefix = getattr(upstream_for_override, "upstream_name", None)
        if upstream_prefix and "/" not in model_to_use.id:
            prefixed_id = f"{upstream_prefix}/{model_to_use.id}"
            if prefixed_id not in aliases:
                aliases.append(prefixed_id)

        # Register a distinct forwarded_model_id as a routable alias.
        if forwarded_model_id and forwarded_model_id not in aliases:
            aliases.append(forwarded_model_id)

        for alias in aliases:
            _add_candidate(alias, model_to_use, upstream_for_override)
        record_unique_model_key(model_to_use)
        seen_model_provider.add(dedupe_key)

    # Sort candidates and build final maps
    model_instances: dict[str, "Model"] = {}
    provider_map: dict[str, list[tuple["Model", "BaseUpstreamProvider"]]] = {}

    def alias_priority(model: "Model", alias: str) -> int:
        """Rank how strong the mapping of alias->model is.

        An exact model ID is authoritative and must be cost-ranked against the
        other exact matches before considering forwarded aliases. This keeps a
        provider-specific forwarded ID from shadowing a directly available,
        cheaper model with the requested ID.
        """
        if model.id and model.id.lower() == alias:
            return 5

        forwarded_model_id = get_effective_forwarded_model_id(model)
        if forwarded_model_id and forwarded_model_id.lower() == alias:
            return 4

        model_base = get_base_model_id(model.id)
        if model_base == alias:
            return 3
        if model.canonical_slug:
            canonical_base = get_base_model_id(model.canonical_slug)
            if canonical_base == alias:
                return 2
        return 1

    for alias, items in candidates.items():
        # Sort key: (priority DESC, cost ASC)
        # Using negative cost for DESC sort overall to keep high priority first
        def sort_key(item: tuple["Model", "BaseUpstreamProvider"]) -> tuple[int, float]:
            model, provider = item
            priority = alias_priority(model, alias)
            cost = calculate_model_cost_score(model)
            penalty = get_provider_penalty(provider)
            adjusted_cost = cost * penalty
            return (priority, -adjusted_cost)

        items.sort(key=sort_key, reverse=True)

        best_model, best_provider = items[0]
        model_instances[alias] = best_model
        provider_map[alias] = list(items)

    # The catalog must advertise the same provider-specific model that routing
    # selects for the public ID. Normally this is the cheapest candidate; alias
    # priority intentionally wins forwarded-ID collisions.
    for unique_key, advertised_id in unique_model_keys.items():
        ranked_candidates = provider_map.get(unique_key)
        if not ranked_candidates:
            continue
        best_model, best_provider = ranked_candidates[0]
        unique_models[unique_key] = best_model.copy(
            update={
                "id": advertised_id,
                "upstream_provider_id": best_provider.provider_type,
                "forwarded_model_id": get_effective_forwarded_model_id(best_model),
            }
        )

    # Log provider distribution (using top provider for stats)
    provider_counts: dict[str, int] = {}
    for candidate_list in provider_map.values():
        if candidate_list:
            provider = candidate_list[0][1]
            provider_name = getattr(provider, "upstream_name", "unknown")
            provider_counts[provider_name] = provider_counts.get(provider_name, 0) + 1

    logger.debug(
        f"Updated model mappings with ({len(unique_models)} unique models and {len(model_instances)} aliases)",
        extra={"provider_distribution": provider_counts},
    )

    return model_instances, provider_map, unique_models
