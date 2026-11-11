"""Describe reservation inputs without repricing a cached model or fetching FX."""

import json
from typing import Any

from ..core.settings import settings
from .models import Model, _calculate_usd_max_costs, has_usable_pricing


def _build_pricing_trail(model: Model | None, base_msats: int) -> dict[str, Any]:
    trail: dict[str, Any] = {
        "base_max_cost_msats": base_msats,
        "required_msats": base_msats,
        "min_request_msat": settings.min_request_msat,
        "min_request_applied": False,
        "discounts": {},
    }
    if settings.fixed_pricing:
        trail.update(base_source="fixed_pricing", formula="fixed_request_cost")
        trail["fixed_cost_per_request_sats"] = settings.fixed_cost_per_request
        trail["min_request_applied"] = (
            settings.fixed_cost_per_request * 1000 < settings.min_request_msat
        )
        return trail
    if model is None or model.sats_pricing is None:
        trail.update(base_source="fallback_fixed", formula="fixed_request_cost")
        trail["fixed_cost_per_request_sats"] = settings.fixed_cost_per_request
        trail["min_request_applied"] = (
            settings.fixed_cost_per_request * 1000 < settings.min_request_msat
        )
        return trail
    if not has_usable_pricing(model.pricing) or not has_usable_pricing(
        model.sats_pricing
    ):
        trail.update(
            base_source="unavailable",
            formula="unavailable",
            error="Model pricing is not usable",
        )
        return trail
    cl = model.top_provider.context_length if model.top_provider else None
    mct = model.top_provider.max_completion_tokens if model.top_provider else None
    if cl and mct:
        formula = "context_plus_completion" if cl > mct else "context_le_completion"
    elif cl:
        formula = "context_only"
    elif mct:
        formula = "completion_only"
    elif model.context_length:
        formula = "model_context_only"
    else:
        formula = "flat_estimate"
    trail.update(
        base_source="model_sats_pricing", formula=formula, max_cost_formula=formula
    )
    trail["max_cost_inputs"] = {
        "context_length": cl,
        "max_completion_tokens": mct,
        "model_context_length": model.context_length,
        **{
            f"{field}_price_usd_per_token"
            if field in ("prompt", "completion")
            else f"{field}_price_usd": getattr(model.pricing, field)
            for field in (
                "prompt",
                "completion",
                "request",
                "image",
                "web_search",
                "internal_reasoning",
            )
        },
    }
    if formula == "flat_estimate":
        trail["max_cost_inputs"]["coefficients"] = dict(
            prompt=1_000_000,
            completion=32_000,
            request=100_000,
            image=100,
            web_search=1000,
            internal_reasoning=100,
        )
    p, c, total = _calculate_usd_max_costs(model)
    trail["max_cost_computed"] = {
        "max_prompt_cost_usd": p,
        "max_completion_cost_usd": c,
        "max_cost_usd": total,
        "cached_max_cost_usd": model.pricing.max_cost,
        "usd_formula_source": "current_model_inputs_not_cached_reservation",
        "max_cost_msats": model.sats_pricing.max_cost * 1000,
        "cached_sats_pricing": model.sats_pricing.dict(),
        # A cache may have been computed at an earlier exchange rate or contain
        # provider-supplied max costs. Do not claim today's FX produced it.
        "exchange_rate_source": "not_recorded_in_model_cache",
    }
    trail["discounts"]["tolerance_percentage"] = settings.tolerance_percentage
    trail["min_request_applied"] = (
        int(
            model.sats_pricing.max_cost
            * 1000
            * (1 - settings.tolerance_percentage / 100)
        )
        < settings.min_request_msat
    )
    return trail


def build_pricing_trail(model: Model | None, base_msats: int) -> dict[str, Any]:
    """Never let diagnostic construction prevent a payment response."""
    try:
        trail = _build_pricing_trail(model, base_msats)
        json.dumps(trail, allow_nan=False)
        return trail
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {
            "base_source": "unavailable",
            "formula": "unavailable",
            "base_max_cost_msats": base_msats,
            "required_msats": base_msats,
            "discounts": {},
            "error": "Model pricing audit inputs are unavailable",
        }
