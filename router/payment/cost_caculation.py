import math
import os

from pydantic.v1 import BaseModel

from ..core.logging import get_logger
from ..core.settings import SettingsManager
from .models import MODELS

logger = get_logger(__name__)

# Cached settings - will be loaded from database
_COST_PER_REQUEST = None
_COST_PER_1K_INPUT_TOKENS = None
_COST_PER_1K_OUTPUT_TOKENS = None
_MODEL_BASED_PRICING = None


async def _get_cost_per_request() -> int:
    """Get cost per request in msats from settings."""
    global _COST_PER_REQUEST
    if _COST_PER_REQUEST is None:
        cost_sats = await SettingsManager.get("COST_PER_REQUEST", 1)
        _COST_PER_REQUEST = cost_sats * 1000  # Convert to msats
    return _COST_PER_REQUEST


async def _get_cost_per_1k_input_tokens() -> int:
    """Get cost per 1K input tokens in msats from settings."""
    global _COST_PER_1K_INPUT_TOKENS
    if _COST_PER_1K_INPUT_TOKENS is None:
        cost_sats = await SettingsManager.get("COST_PER_1K_INPUT_TOKENS", 0)
        _COST_PER_1K_INPUT_TOKENS = cost_sats * 1000  # Convert to msats
    return _COST_PER_1K_INPUT_TOKENS


async def _get_cost_per_1k_output_tokens() -> int:
    """Get cost per 1K output tokens in msats from settings."""
    global _COST_PER_1K_OUTPUT_TOKENS
    if _COST_PER_1K_OUTPUT_TOKENS is None:
        cost_sats = await SettingsManager.get("COST_PER_1K_OUTPUT_TOKENS", 0)
        _COST_PER_1K_OUTPUT_TOKENS = cost_sats * 1000  # Convert to msats
    return _COST_PER_1K_OUTPUT_TOKENS


async def _is_model_based_pricing() -> bool:
    """Check if model-based pricing is enabled from settings."""
    global _MODEL_BASED_PRICING
    if _MODEL_BASED_PRICING is None:
        _MODEL_BASED_PRICING = await SettingsManager.get("MODEL_BASED_PRICING", False)
    return _MODEL_BASED_PRICING


# Cache reload function
async def reload_pricing_settings() -> None:
    """Reload pricing settings from database."""
    global _COST_PER_REQUEST, _COST_PER_1K_INPUT_TOKENS, _COST_PER_1K_OUTPUT_TOKENS, _MODEL_BASED_PRICING
    _COST_PER_REQUEST = None
    _COST_PER_1K_INPUT_TOKENS = None
    _COST_PER_1K_OUTPUT_TOKENS = None
    _MODEL_BASED_PRICING = None
    
    # Log the updated settings
    logger.info(
        "Pricing settings reloaded",
        extra={
            "cost_per_request": await _get_cost_per_request(),
            "cost_per_1k_input_tokens": await _get_cost_per_1k_input_tokens(),
            "cost_per_1k_output_tokens": await _get_cost_per_1k_output_tokens(),
            "model_based_pricing": await _is_model_based_pricing(),
        },
    )


class CostData(BaseModel):
    base_msats: int
    input_msats: int
    output_msats: int
    total_msats: int


class MaxCostData(CostData):
    pass


class CostDataError(BaseModel):
    message: str
    code: str


async def calculate_cost(
    response_data: dict, max_cost: int
) -> CostData | MaxCostData | CostDataError:
    """
    Calculate the cost of an API request based on token usage.

    Args:
        response_data: Response data containing usage information
        max_cost: Maximum cost in millisats

    Returns:
        Cost data or error information
    """
    logger.debug(
        "Starting cost calculation",
        extra={
            "max_cost_msats": max_cost,
            "has_usage_data": "usage" in response_data,
            "response_model": response_data.get("model", "unknown"),
        },
    )

    cost_data = MaxCostData(
        base_msats=max_cost,
        input_msats=0,
        output_msats=0,
        total_msats=max_cost,
    )

    if "usage" not in response_data or response_data["usage"] is None:
        logger.warning(
            "No usage data in response, using base cost only",
            extra={
                "max_cost_msats": max_cost,
                "model": response_data.get("model", "unknown"),
            },
        )
        return cost_data

    MSATS_PER_1K_INPUT_TOKENS = await _get_cost_per_1k_input_tokens()
    MSATS_PER_1K_OUTPUT_TOKENS = await _get_cost_per_1k_output_tokens()

    if await _is_model_based_pricing() and MODELS:
        response_model = response_data.get("model", "")
        logger.debug(
            "Using model-based pricing",
            extra={
                "model": response_model,
                "available_models": [model.id for model in MODELS],
            },
        )

        if response_model not in [model.id for model in MODELS]:
            logger.error(
                "Invalid model in response",
                extra={
                    "response_model": response_model,
                    "available_models": [model.id for model in MODELS],
                },
            )
            return CostDataError(
                message=f"Invalid model in response: {response_model}",
                code="model_not_found",
            )

        model = next(model for model in MODELS if model.id == response_model)
        if model.sats_pricing is None:
            logger.error(
                "Model pricing not defined",
                extra={"model": response_model, "model_id": model.id},
            )
            return CostDataError(
                message="Model pricing not defined", code="pricing_not_found"
            )

        MSATS_PER_1K_INPUT_TOKENS = model.sats_pricing.prompt * 1_000_000  # type: ignore
        MSATS_PER_1K_OUTPUT_TOKENS = model.sats_pricing.completion * 1_000_000  # type: ignore

        logger.info(
            "Applied model-specific pricing",
            extra={
                "model": response_model,
                "input_price_msats_per_1k": MSATS_PER_1K_INPUT_TOKENS,
                "output_price_msats_per_1k": MSATS_PER_1K_OUTPUT_TOKENS,
            },
        )

    if not (MSATS_PER_1K_OUTPUT_TOKENS and MSATS_PER_1K_INPUT_TOKENS):
        logger.warning(
            "No token pricing configured, using base cost",
            extra={
                "base_cost_msats": max_cost,
                "model": response_data.get("model", "unknown"),
            },
        )
        return cost_data

    input_tokens = response_data.get("usage", {}).get("prompt_tokens", 0)
    output_tokens = response_data.get("usage", {}).get("completion_tokens", 0)

    input_msats = round(input_tokens / 1000 * MSATS_PER_1K_INPUT_TOKENS, 3)
    output_msats = round(output_tokens / 1000 * MSATS_PER_1K_OUTPUT_TOKENS, 3)
    token_based_cost = math.ceil(input_msats + output_msats)

    logger.info(
        "Calculated token-based cost",
        extra={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_cost_msats": input_msats,
            "output_cost_msats": output_msats,
            "total_cost_msats": token_based_cost,
            "model": response_data.get("model", "unknown"),
        },
    )

    return CostData(
        base_msats=0,
        input_msats=int(input_msats),
        output_msats=int(output_msats),
        total_msats=token_based_cost,
    )
