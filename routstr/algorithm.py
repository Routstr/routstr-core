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
    overrides_by_id: dict[str, tuple],
    disabled_model_ids: set[str],
) -> tuple[
    dict[str, "Model"], dict[str, list["BaseUpstreamProvider"]], dict[str, "Model"]
]:
    """Create optimal model mappings based on cost and provider preferences.

    This is the main entry point for the algorithm. It processes all upstream providers
    and creates three mappings based on cost optimization:

    1. model_instances: alias -> Model (all model aliases mapped to their Model objects)
    2. provider_map: alias -> List[UpstreamProvider] (sorted list of providers for each alias)
    3. unique_models: base_id -> Model (unique models without provider prefixes)

    The algorithm:
    - Processes non-OpenRouter providers first (they're typically cheaper)
    - Then processes OpenRouter models (they can still win if cheaper)
    - For each model alias, collects all candidates and sorts them by priority and cost.

    Args:
        upstreams: List of all upstream provider instances
        overrides_by_id: Dict of model overrides from database {model_id: (ModelRow, fee)}
        disabled_model_ids: Set of model IDs that should be excluded

    Returns:
        Tuple of (model_instances, provider_map, unique_models)
    """
    from .payment.models import _row_to_model
    from .upstream.helpers import resolve_model_alias

    candidates: dict[str, list[tuple["Model", "BaseUpstreamProvider"]]] = {}
    unique_models: dict[str, "Model"] = {}

    # Separate OpenRouter from other providers
    openrouter: "BaseUpstreamProvider" | None = None
    other_upstreams: list["BaseUpstreamProvider"] = []

    for upstream in upstreams:
        base_url = getattr(upstream, "base_url", "")
        if base_url == "https://openrouter.ai/api/v1":
            openrouter = upstream
        else:
            other_upstreams.append(upstream)

    def get_base_model_id(model_id: str) -> str:
        """Get base model ID by removing provider prefix."""
        return model_id.split("/", 1)[1] if "/" in model_id else model_id

    def _add_candidate(
        alias: str, model: "Model", provider: "BaseUpstreamProvider"
    ) -> None:
        """Add candidate model/provider for an alias."""
        alias_lower = alias.lower()
        if alias_lower not in candidates:
            candidates[alias_lower] = []
        candidates[alias_lower].append((model, provider))

    def process_provider_models(
        upstream: "BaseUpstreamProvider", is_openrouter: bool = False
    ) -> None:
        """Process all models from a given provider."""
        upstream_prefix = getattr(upstream, "upstream_name", None)

        for model in upstream.get_cached_models():
            if not model.enabled or model.id in disabled_model_ids:
                continue

            # Apply overrides if present
            if model.id in overrides_by_id:
                override_row, provider_fee = overrides_by_id[model.id]
                model_to_use = _row_to_model(
                    override_row, apply_provider_fee=True, provider_fee=provider_fee
                )
            else:
                model_to_use = model

            # Add to unique models
            base_id = get_base_model_id(model_to_use.id)
            if not is_openrouter or base_id not in unique_models:
                unique_model = model_to_use.copy(
                    update={
                        "id": base_id,
                        "upstream_provider_id": upstream.provider_type,
                    }
                )
                unique_models[base_id] = unique_model

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

            # Try to set each alias
            for alias in aliases:
                _add_candidate(alias, model_to_use, upstream)

    # Process non-OpenRouter providers first
    for upstream in other_upstreams:
        process_provider_models(upstream, is_openrouter=False)

    # Process OpenRouter last
    if openrouter:
        process_provider_models(openrouter, is_openrouter=True)

    # Sort candidates and build final maps
    model_instances: dict[str, "Model"] = {}
    provider_map: dict[str, list["BaseUpstreamProvider"]] = {}

    def alias_priority(model: "Model", alias: str) -> int:
        """Rank how strong the mapping of alias->model is."""
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
        provider_map[alias] = [p for _, p in items]

    # Log provider distribution (using top provider for stats)
    provider_counts: dict[str, int] = {}
    for providers in provider_map.values():
        if providers:
            provider = providers[0]
            provider_name = getattr(provider, "upstream_name", "unknown")
            provider_counts[provider_name] = provider_counts.get(provider_name, 0) + 1

    logger.debug(
        f"Updated model mappings with ({len(unique_models)} unique models and {len(model_instances)} aliases)",
        extra={"provider_distribution": provider_counts},
    )

    return model_instances, provider_map, unique_models
