import math

from pydantic.v1 import BaseModel

from ..core import get_logger
from ..core.db import AsyncSession
from ..core.settings import settings

logger = get_logger(__name__)


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
    response_data: dict, max_cost: int, session: AsyncSession
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

    MSATS_PER_1K_INPUT_TOKENS: float = (
        float(settings.fixed_per_1k_input_tokens) * 1000.0
    )
    MSATS_PER_1K_OUTPUT_TOKENS: float = (
        float(settings.fixed_per_1k_output_tokens) * 1000.0
    )

    if not settings.fixed_pricing:
        response_model = response_data.get("model", "")
        logger.debug(
            "Using model-based pricing",
            extra={"model": response_model},
        )

        from ..proxy import get_upstreams
        from ..upstream import get_model_with_override

        upstreams = get_upstreams()
        model_obj = await get_model_with_override(
            response_model, upstreams, session=session
        )

        if not model_obj:
            logger.error(
                "Invalid model in response",
                extra={"response_model": response_model},
            )
            return CostDataError(
                message=f"Invalid model in response: {response_model}",
                code="model_not_found",
            )

        if not model_obj.sats_pricing:
            logger.error(
                "Model pricing not defined",
                extra={"model": response_model, "model_id": response_model},
            )
            return CostDataError(
                message="Model pricing not defined", code="pricing_not_found"
            )

        try:
            mspp = float(model_obj.sats_pricing.prompt)
            mspc = float(model_obj.sats_pricing.completion)
        except Exception:
            return CostDataError(message="Invalid pricing data", code="pricing_invalid")

        MSATS_PER_1K_INPUT_TOKENS = mspp * 1_000_000.0
        MSATS_PER_1K_OUTPUT_TOKENS = mspc * 1_000_000.0

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
