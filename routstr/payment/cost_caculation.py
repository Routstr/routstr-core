import base64
import math
import struct
from typing import Optional

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


def get_image_resolution_from_data_url(data_url: str) -> Optional[tuple[int, int]]:
    """
    Extract image resolution (width, height) from a base64 data URL without DOM.
    Supports PNG and JPEG. Returns None if format unsupported or parsing fails.
    """
    try:
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            return None

        comma_idx = data_url.find(",")
        if comma_idx == -1:
            return None

        meta = data_url[5:comma_idx]  # e.g. "image/png;base64"
        base64_data = data_url[comma_idx + 1:]

        # Decode base64 to binary
        try:
            binary_data = base64.b64decode(base64_data)
        except Exception:
            return None

        is_png = "image/png" in meta
        is_jpeg = "image/jpeg" in meta or "image/jpg" in meta

        # PNG: width/height are 4-byte big-endian at offsets 16 and 20
        if is_png:
            # Validate PNG signature
            png_sig = b'\x89PNG\r\n\x1a\n'
            if not binary_data.startswith(png_sig):
                return None

            if len(binary_data) < 24:
                return None

            # Width and height are at bytes 16-19 and 20-23 respectively
            width = struct.unpack('>I', binary_data[16:20])[0]
            height = struct.unpack('>I', binary_data[20:24])[0]

            if width > 0 and height > 0:
                return (width, height)
            return None

        # JPEG: parse markers to SOF0/SOF2 for dimensions
        if is_jpeg:
            offset = 0
            # JPEG SOI 0xFFD8
            if len(binary_data) < 2 or binary_data[0] != 0xFF or binary_data[1] != 0xD8:
                return None
            offset = 2

            while offset < len(binary_data):
                # Find marker
                while offset < len(binary_data) and binary_data[offset] != 0xFF:
                    offset += 1
                if offset + 1 >= len(binary_data):
                    break

                # Skip fill bytes 0xFF
                while offset < len(binary_data) and binary_data[offset] == 0xFF:
                    offset += 1
                if offset >= len(binary_data):
                    break

                marker = binary_data[offset]
                offset += 1

                # Standalone markers without length
                if marker == 0xD8 or marker == 0xD9:  # SOI/EOI
                    continue

                if offset + 1 >= len(binary_data):
                    break

                length = (binary_data[offset] << 8) | binary_data[offset + 1]
                offset += 2

                # SOF0 (0xC0) or SOF2 (0xC2) contain dimensions
                if marker == 0xC0 or marker == 0xC2:
                    if length < 7 or offset + length - 2 > len(binary_data):
                        return None
                    # Skip precision byte
                    height = (binary_data[offset + 1] << 8) | binary_data[offset + 2]
                    width = (binary_data[offset + 3] << 8) | binary_data[offset + 4]
                    if width > 0 and height > 0:
                        return (width, height)
                    return None
                else:
                    # Skip this segment
                    offset += length - 2

            return None

        # Unsupported formats (e.g., webp/gif) - skip for now
        return None
    except Exception:
        return None


def calculate_image_tokens_from_messages(messages: list) -> int:
    """
    Calculate image tokens from messages using 32px patch method.
    """
    image_tokens = 0

    try:
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if (isinstance(part, dict) and
                        part.get("type") == "image_url"):

                        image_url = part.get("image_url")
                        url: Optional[str] = None
                        if isinstance(image_url, str):
                            url = image_url
                        elif isinstance(image_url, dict):
                            url = image_url.get("url")
                        else:
                            continue

                        # Expecting a base64 data URL for local image inputs
                        if url and isinstance(url, str) and url.startswith("data:"):
                            resolution = get_image_resolution_from_data_url(url)
                            if resolution:
                                width, height = resolution
                                patch_size = 32
                                patches_w = (width + patch_size - 1) // patch_size
                                patches_h = (height + patch_size - 1) // patch_size
                                tokens_from_image = patches_w * patches_h
                                image_tokens += tokens_from_image

                                logger.debug(
                                    "Calculated image tokens",
                                    extra={
                                        "width": width,
                                        "height": height,
                                        "tokens_from_image": tokens_from_image,
                                    }
                                )
                            else:
                                logger.warning(
                                    "Could not determine image resolution",
                                    extra={"url_prefix": url[:50] + "..." if len(url) > 50 else url}
                                )
    except Exception as e:
        logger.error(
            "Error calculating image tokens",
            extra={"error": str(e), "error_type": type(e).__name__}
        )

    return image_tokens


async def calculate_cost(  # todo: can be sync
    response_data: dict, max_cost: int, session: AsyncSession, request_data: Optional[dict] = None
) -> CostData | MaxCostData | CostDataError:
    """
    Calculate the cost of an API request based on token usage.

    Args:
        response_data: Response data containing usage information
        max_cost: Maximum cost in millisats
        session: Database session
        request_data: Original request data containing messages (for image token calculation)

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

    # Calculate image tokens from request data if available
    image_tokens = 0
    if request_data and "messages" in request_data:
        image_tokens = calculate_image_tokens_from_messages(request_data["messages"])
        logger.debug(
            "Image tokens calculated",
            extra={"image_tokens": image_tokens, "text_input_tokens": input_tokens}
        )

    # Add image tokens to input tokens for cost calculation
    total_input_tokens = input_tokens + image_tokens

    input_msats = round(total_input_tokens / 1000 * MSATS_PER_1K_INPUT_TOKENS, 3)
    output_msats = round(output_tokens / 1000 * MSATS_PER_1K_OUTPUT_TOKENS, 3)
    token_based_cost = math.ceil(input_msats + output_msats)

    logger.info(
        "Calculated token-based cost",
        extra={
            "text_input_tokens": input_tokens,
            "image_tokens": image_tokens,
            "total_input_tokens": total_input_tokens,
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
