import base64
import json
import math
from io import BytesIO
from typing import Any

import httpx
from fastapi import HTTPException, Response
from fastapi.requests import Request
from PIL import Image
from sqlmodel.ext.asyncio.session import AsyncSession

from ..core import get_logger
from ..core.settings import settings
from ..wallet import deserialize_token_from_string

logger = get_logger(__name__)


def check_token_balance(headers: dict, body: dict, max_cost_for_model: int) -> None:
    if x_cashu := headers.get("x-cashu", None):
        cashu_token = x_cashu
        logger.debug(
            "Using X-Cashu token",
            extra={
                "token_preview": cashu_token[:20] + "..."
                if len(cashu_token) > 20
                else cashu_token
            },
        )
    elif auth := headers.get("authorization", None):
        cashu_token = auth.split(" ")[1] if len(auth.split(" ")) > 1 else ""
        logger.debug(
            "Using Authorization header token",
            extra={
                "token_preview": cashu_token[:20] + "..."
                if len(cashu_token) > 20
                else cashu_token
            },
        )
    else:
        logger.error("No authentication token provided")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Handle empty token
    if not cashu_token:
        logger.error("Empty token provided")
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "API key or Cashu token required",
                    "type": "invalid_request_error",
                    "code": "missing_api_key",
                }
            },
        )

    # Handle regular API keys (sk-*)
    if cashu_token.startswith("sk-"):
        return

    try:
        token_obj = deserialize_token_from_string(cashu_token)
    except Exception:
        # Invalid token format - let the auth system handle it
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication token format",
        )

    amount_msat = (
        token_obj.amount if token_obj.unit == "msat" else token_obj.amount * 1000
    )

    if max_cost_for_model > amount_msat:
        raise HTTPException(
            status_code=413,
            detail={
                "reason": "Insufficient balance",
                "amount_required_msat": max_cost_for_model,
                "model": body.get("model", "unknown"),
                "type": "minimum_balance_required",
            },
        )


async def get_max_cost_for_model(
    model: str,
    session: AsyncSession,
    model_obj: Any | None = None,
) -> int:
    """Get the maximum cost for a specific model from providers with overrides."""
    logger.debug(
        "Getting max cost for model",
        extra={
            "model": model,
            "fixed_pricing": settings.fixed_pricing,
        },
    )

    if settings.fixed_pricing:
        default_cost_msats = settings.fixed_cost_per_request * 1000
        logger.debug(
            "Using fixed cost pricing",
            extra={"cost_msats": default_cost_msats, "model": model},
        )
        return max(settings.min_request_msat, default_cost_msats)

    if not model_obj:
        from ..proxy import get_model_instance

        model_obj = get_model_instance(model)

    if not model_obj:
        fallback_msats = settings.fixed_cost_per_request * 1000
        logger.warning(
            "Model not found in providers or overrides",
            extra={
                "requested_model": model,
                "using_default_cost": fallback_msats,
            },
        )
        return max(settings.min_request_msat, fallback_msats)

    if model_obj.sats_pricing:
        try:
            max_cost = (
                model_obj.sats_pricing.max_cost
                * 1000
                * (1 - settings.tolerance_percentage / 100)
            )
            logger.debug(
                "Found model-specific max cost",
                extra={"model": model, "max_cost_msats": max_cost},
            )
            calculated_msats = int(max_cost)
            return max(settings.min_request_msat, calculated_msats)
        except Exception as e:
            logger.error(
                "Error calculating max cost from model pricing",
                extra={"model": model, "error": str(e)},
            )

    logger.warning(
        "Model pricing not found, using fixed cost",
        extra={
            "model": model,
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
        prompt_tokens = estimate_tokens(messages)

        image_tokens = await estimate_image_tokens_in_messages(messages)
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


def estimate_tokens(messages: list) -> int:
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


async def estimate_image_tokens_in_messages(messages: list) -> int:
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


def create_error_response(
    error_type: str,
    message: str,
    status_code: int,
    request: Request,
    token: str | None = None,
) -> Response:
    """Create a standardized error response."""
    return Response(
        content=json.dumps(
            {
                "error": {
                    "message": message,
                    "type": error_type,
                    "code": status_code,
                },
                "request_id": getattr(request.state, "request_id", "unknown"),
            }
        ),
        status_code=status_code,
        media_type="application/json",
        headers={"X-Cashu": token} if token else {},
    )
