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


def should_prefer_model(
    candidate_model: "Model",
    candidate_provider: "BaseUpstreamProvider",
    current_model: "Model",
    current_provider: "BaseUpstreamProvider",
    alias: str,
) -> bool:
    """Determine if candidate model should replace current model for an alias.

    This is the core decision function for model prioritization. It considers:
    1. Alias matching quality (exact match vs. canonical slug match)
    2. Model cost (lower is better)
    3. Provider penalties (e.g., slight preference against OpenRouter)

    Args:
        candidate_model: The new model being considered
        candidate_provider: Provider offering the candidate model
        current_model: The currently selected model for this alias
        current_provider: Provider offering the current model
        alias: The model alias being mapped

    Returns:
        True if candidate should replace current, False otherwise
    """

    def get_base_model_id(model_id: str) -> str:
        """Get base model ID by removing provider prefix."""
        return model_id.split("/", 1)[1] if "/" in model_id else model_id

    def alias_priority(model: "Model") -> int:
        """Rank how strong the mapping of alias->model is.

        Highest priority when alias exactly equals the model ID without provider prefix.
        Next when alias equals canonical slug without prefix. Otherwise lowest.
        """
        model_base = get_base_model_id(model.id)
        if model_base == alias:
            return 3
        if model.canonical_slug:
            canonical_base = get_base_model_id(model.canonical_slug)
            if canonical_base == alias:
                return 2
        return 1

    candidate_alias_priority = alias_priority(candidate_model)
    current_alias_priority = alias_priority(current_model)

    # If candidate has better alias match, prefer it regardless of cost
    if candidate_alias_priority > current_alias_priority:
        return True

    # If current has better alias match, keep it regardless of cost
    if current_alias_priority > candidate_alias_priority:
        return False

    # Same alias priority - compare costs
    candidate_cost = calculate_model_cost_score(candidate_model)
    current_cost = calculate_model_cost_score(current_model)

    # Apply provider penalties
    candidate_adjusted = candidate_cost * get_provider_penalty(candidate_provider)
    current_adjusted = current_cost * get_provider_penalty(current_provider)

    # Prefer lower adjusted cost
    should_replace = candidate_adjusted < current_adjusted

    # Log provider changes when candidate wins
    if should_replace:
        candidate_provider_name = getattr(
            candidate_provider, "upstream_name", "unknown"
        )
        current_provider_name = getattr(current_provider, "upstream_name", "unknown")
        logger.debug(
            f"Model selection for alias '{alias}': choosing {candidate_provider_name} "
            f"(cost: ${candidate_adjusted:.6f}) over {current_provider_name} "
            f"(cost: ${current_adjusted:.6f})"
        )

    return should_replace


def create_model_mappings(
    upstreams: list["BaseUpstreamProvider"],
    overrides_by_id: dict[str, tuple],
    disabled_model_ids: set[str],
) -> tuple[dict[str, "Model"], dict[str, "BaseUpstreamProvider"], dict[str, "Model"]]:
    """Create optimal model mappings based on cost and provider preferences.

    This is the main entry point for the algorithm. It processes all upstream providers
    and creates three mappings based on cost optimization:

    1. model_instances: alias -> Model (all model aliases mapped to their Model objects)
    2. provider_map: alias -> UpstreamProvider (which provider to use for each alias)
    3. unique_models: base_id -> Model (unique models without provider prefixes)

    The algorithm:
    - Processes non-OpenRouter providers first (they're typically cheaper)
    - Then processes OpenRouter models (they can still win if cheaper)
    - For each model alias, uses should_prefer_model() to select the best provider

    Args:
        upstreams: List of all upstream provider instances
        overrides_by_id: Dict of model overrides from database {model_id: (ModelRow, fee)}
        disabled_model_ids: Set of model IDs that should be excluded

    Returns:
        Tuple of (model_instances, provider_map, unique_models)
    """
    from .payment.models import _row_to_model
    from .upstream import resolve_model_alias

    model_instances: dict[str, "Model"] = {}
    provider_map: dict[str, "BaseUpstreamProvider"] = {}
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

    def _maybe_set_alias(
        alias: str, model: "Model", provider: "BaseUpstreamProvider"
    ) -> None:
        """Set alias to model/provider if not set or if new model is preferred."""
        existing_model = model_instances.get(alias)
        if not existing_model:
            # No existing mapping, set it
            model_instances[alias] = model
            provider_map[alias] = provider
        else:
            # Check if candidate should replace existing
            existing_provider = provider_map[alias]
            if should_prefer_model(
                model, provider, existing_model, existing_provider, alias
            ):
                model_instances[alias] = model
                provider_map[alias] = provider

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
                unique_model = model_to_use.copy(update={"id": base_id})
                unique_models[base_id] = unique_model

            # Get all aliases for this model
            aliases = resolve_model_alias(model_to_use.id, model_to_use.canonical_slug)

            # Add prefixed alias if applicable
            if upstream_prefix and "/" not in model_to_use.id:
                prefixed_id = f"{upstream_prefix}/{model_to_use.id}"
                if prefixed_id not in aliases:
                    aliases.append(prefixed_id)

            # Try to set each alias
            for alias in aliases:
                _maybe_set_alias(alias, model_to_use, upstream)

    # Process non-OpenRouter providers first (they're typically cheaper)
    for upstream in other_upstreams:
        process_provider_models(upstream, is_openrouter=False)

    # Process OpenRouter last - models only win if they're cheaper or better matched
    if openrouter:
        process_provider_models(openrouter, is_openrouter=True)

    # Log provider distribution
    provider_counts: dict[str, int] = {}
    for provider in provider_map.values():
        provider_name = getattr(provider, "upstream_name", "unknown")
        provider_counts[provider_name] = provider_counts.get(provider_name, 0) + 1

    logger.debug(
        "Created model mappings",
        extra={
            "unique_model_count": len(unique_models),
            "total_alias_count": len(model_instances),
            "provider_distribution": provider_counts,
        },
    )

    return model_instances, provider_map, unique_models
