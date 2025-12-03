import base64
import math
from io import BytesIO
from typing import Any

import httpx
from PIL import Image
from pydantic.v1 import BaseModel

from ..core import get_logger
from ..core.settings import settings
from ..models.models import Model

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


def calculate_cost(
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

        from ..proxy import get_model_instance

        model_obj = get_model_instance(response_model)

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


def get_max_cost_for_model(model_obj: Model) -> int:
    """Get the maximum cost for a specific model from providers with overrides."""
    if settings.fixed_pricing:
        default_cost_msats = settings.fixed_cost_per_request * 1000
        return max(settings.min_request_msat, default_cost_msats)

    if model_obj.sats_pricing:
        max_cost = (
            model_obj.sats_pricing.max_cost
            * 1000
            * (1 - settings.tolerance_percentage / 100)
        )
        calculated_msats = int(max_cost)
        return max(settings.min_request_msat, calculated_msats)

    logger.warning(
        "Model pricing not found, using fixed cost",
        extra={
            "model": model_obj.id,
            "default_cost_msats": settings.fixed_cost_per_request * 1000,
        },
    )
    return max(settings.min_request_msat, settings.fixed_cost_per_request * 1000)


async def calculate_discounted_max_cost(
    max_cost_for_model: int,
    body: dict,
    model_obj: Any | None = None,
) -> int:
    """Calculate the discounted max cost for a request using model pricing when available."""
    if settings.fixed_pricing:
        return max_cost_for_model

    model = body.get("model", "unknown")

    model_pricing = model_obj.sats_pricing if model_obj else None
    if not model_pricing:
        return max_cost_for_model

    tol = settings.tolerance_percentage
    tol_factor = max(0.0, 1 - float(tol) / 100.0)
    max_prompt_allowed_sats = model_pricing.max_prompt_cost * tol_factor
    max_completion_allowed_sats = model_pricing.max_completion_cost * tol_factor

    adjusted = max_cost_for_model

    if messages := body.get("messages"):
        prompt_tokens = _estimate_tokens(messages)

        image_tokens = await _estimate_image_tokens_in_messages(messages)
        if image_tokens > 0:
            logger.debug(
                "Found images in request",
                extra={
                    "model": model,
                    "image_tokens": image_tokens,
                },
            )
            prompt_tokens += image_tokens

        estimated_prompt_delta_sats = (
            max_prompt_allowed_sats - prompt_tokens * model_pricing.prompt
        )
        if estimated_prompt_delta_sats > 0:
            adjusted = adjusted - math.floor(estimated_prompt_delta_sats * 1000)

    max_tokens_raw = body.get("max_tokens", None)
    if max_tokens_raw is not None:
        try:
            max_tokens_int = int(max_tokens_raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid max_tokens; ignoring in cost adjustment",
                extra={"max_tokens": str(max_tokens_raw)[:64], "model": model},
            )
        else:
            estimated_completion_delta_sats = (
                max_completion_allowed_sats - max_tokens_int * model_pricing.completion
            )
            if estimated_completion_delta_sats > 0:
                adjusted = adjusted - math.floor(estimated_completion_delta_sats * 1000)

    logger.debug(
        "Discounted max cost computed",
        extra={
            "model": model,
            "original_msats": max_cost_for_model,
            "adjusted_msats": adjusted,
            "tolerance_pct": tol,
        },
    )

    return max(0, adjusted)


def _estimate_tokens(messages: list) -> int:
    """Estimate tokens for text content, excluding image_url fields."""
    total = 0
    for msg in messages:
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                total += sum(
                    len(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
    return total // 3


def _get_image_dimensions(image_data: bytes) -> tuple[int, int]:
    """Extract image dimensions from image bytes."""
    try:
        img = Image.open(BytesIO(image_data))
        return img.size
    except Exception as e:
        logger.warning(
            "Failed to get image dimensions, using default",
            extra={"error": str(e)},
        )
        return (512, 512)


async def _fetch_image_from_url(url: str) -> bytes | None:
    """Fetch image from URL."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    except Exception as e:
        logger.warning(
            "Failed to fetch image from URL",
            extra={"error": str(e), "url": url[:100]},
        )
        return None


def _calculate_image_tokens(width: int, height: int, detail: str = "auto") -> int:
    """Calculate image tokens based on OpenAI's vision pricing.

    For low detail: 85 tokens
    For high detail/auto: 85 base tokens + 170 tokens per 512px tile
    """
    if detail == "low":
        return 85

    if width > 2048 or height > 2048:
        aspect_ratio = width / height
        if width > height:
            width = 2048
            height = int(width / aspect_ratio)
        else:
            height = 2048
            width = int(height * aspect_ratio)

    if width > 768 or height > 768:
        aspect_ratio = width / height
        if width > height:
            width = 768
            height = int(width / aspect_ratio)
        else:
            height = 768
            width = int(height * aspect_ratio)

    tiles_width = (width + 511) // 512
    tiles_height = (height + 511) // 512
    num_tiles = tiles_width * tiles_height

    return 85 + (170 * num_tiles)


async def _estimate_image_tokens_in_messages(messages: list) -> int:
    """Estimate total tokens for all images in messages.

    Supports both base64 encoded images and image URLs.
    """
    total_image_tokens = 0

    for message in messages:
        if not isinstance(message, dict):
            continue

        content = message.get("content")
        if not content:
            continue

        if isinstance(content, str):
            continue

        if not isinstance(content, list):
            continue

        for content_item in content:
            if not isinstance(content_item, dict):
                continue

            content_type = content_item.get("type")
            if content_type not in ("image_url", "input_image"):
                continue

            image_url_data = content_item.get("image_url")
            if not image_url_data:
                continue

            if isinstance(image_url_data, str):
                url = image_url_data
                detail = "auto"
            elif isinstance(image_url_data, dict):
                url = image_url_data.get("url", "")
                detail = image_url_data.get("detail", "auto")
            else:
                continue

            if not url:
                continue

            if url.startswith("data:image/"):
                try:
                    header, base64_data = url.split(",", 1)
                    image_bytes = base64.b64decode(base64_data)
                    width, height = _get_image_dimensions(image_bytes)
                    tokens = _calculate_image_tokens(width, height, detail)
                    total_image_tokens += tokens
                    logger.debug(
                        "Calculated tokens for base64 image",
                        extra={
                            "width": width,
                            "height": height,
                            "detail": detail,
                            "tokens": tokens,
                        },
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to process base64 image",
                        extra={"error": str(e)},
                    )
                    total_image_tokens += 85
            else:
                image_bytes_or_none = await _fetch_image_from_url(url)
                if image_bytes_or_none:
                    width, height = _get_image_dimensions(image_bytes_or_none)
                    tokens = _calculate_image_tokens(width, height, detail)
                    total_image_tokens += tokens
                    logger.debug(
                        "Calculated tokens for URL image",
                        extra={
                            "url": url[:100],
                            "width": width,
                            "height": height,
                            "detail": detail,
                            "tokens": tokens,
                        },
                    )
                else:
                    total_image_tokens += 85

    return total_image_tokens
