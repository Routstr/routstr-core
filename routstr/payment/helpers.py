import asyncio
import base64
import ipaddress
import json
import math
import socket
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException, Response
from fastapi.requests import Request
from PIL import Image
from sqlmodel.ext.asyncio.session import AsyncSession

from ..core import get_logger
from ..core.exceptions import UpstreamError
from ..core.redaction import redact_org_ids
from ..core.settings import settings
from ..wallet import (
    UntrustedSourceMintError,
    classify_redemption_error,
    deserialize_token_from_string,
    is_trusted_source_mint,
)

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
        logger.debug(
            "Skipping preflight token balance check for Authorization header",
            extra={
                "auth_preview": auth[:20] + "..." if len(auth) > 20 else auth,
            },
        )
        return
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

    if not is_trusted_source_mint(token_obj.mint):
        classified = classify_redemption_error(
            UntrustedSourceMintError(f"Untrusted source mint: {token_obj.mint}")
        )
        assert classified is not None
        error_type, status_code, message, error_code = classified
        raise HTTPException(
            status_code=status_code,
            detail={
                "error": {"message": message, "type": error_type, "code": error_code}
            },
        )

    amount_msat = (
        token_obj.amount if token_obj.unit == "msat" else token_obj.amount * 1000
    )

    if max_cost_for_model > amount_msat:
        raise HTTPException(
            status_code=402,
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

    if model_obj:
        prompt_token_limit: int | None = None
        if model_obj.top_provider and (
            model_obj.top_provider.context_length
            or model_obj.top_provider.max_completion_tokens
        ):
            cl = model_obj.top_provider.context_length
            mct = model_obj.top_provider.max_completion_tokens
            if cl and mct:
                prompt_token_limit = max(0, cl - mct)
            elif cl:
                prompt_token_limit = cl
            elif mct:
                prompt_token_limit = 0
        elif model_obj.context_length:
            prompt_token_limit = model_obj.context_length

        if prompt_token_limit is not None:
            max_prompt_allowed_sats = (
                prompt_token_limit * model_pricing.prompt * tol_factor
            )

    adjusted = max_cost_for_model

    messages = body.get("messages")
    # Estimated over the whole body: a discount driven by message text alone lets
    # a caller hide prompt weight elsewhere, shrink the reservation, and be billed
    # for work the reservation never covered.
    prompt_tokens = estimate_prompt_tokens(body)

    if isinstance(messages, list):
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

    if prompt_tokens > 0:
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

    return max(settings.min_request_msat, adjusted)


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


def _sum_string_chars(node: Any) -> int:
    """Recursively sum the length of every string in the tree, keys included.

    Nothing is excluded. Keys count because JSON-schema property names are
    forwarded to the provider, and no exclusion rule can be trusted here: every
    part of the body is caller-controlled, so any carve-out (by key name or by
    value shape) is a place to hide prompt weight for free. Inline image data is
    therefore counted as text too, which only makes the discount smaller.
    """
    if isinstance(node, str):
        return len(node)
    if isinstance(node, dict):
        return sum(
            len(str(key)) + _sum_string_chars(value) for key, value in node.items()
        )
    if isinstance(node, list):
        return sum(_sum_string_chars(item) for item in node)
    return 0


def _count_prompt_token_ids(node: Any) -> int:
    if isinstance(node, int) and not isinstance(node, bool):
        return 1
    if isinstance(node, list):
        return sum(_count_prompt_token_ids(item) for item in node)
    return 0


def estimate_prompt_tokens(body: dict) -> int:
    """Conservatively estimate prompt tokens for the whole provider-bound body.

    Every string counts, as do token IDs in legacy ``prompt`` arrays, so no
    forwarded field can hide prompt weight and shrink its reservation.
    """
    return _sum_string_chars(body) // 3 + _count_prompt_token_ids(body.get("prompt"))


IMAGE_FETCH_TIMEOUT_SECONDS = 10.0
# Dimensions live in the header, so a prefix suffices and an endless body cannot
# pin memory.
IMAGE_FETCH_MAX_BYTES = 512 * 1024
# Fetches are sequential, so an unbounded URL list is a request-time amplifier.
IMAGE_FETCH_MAX_PER_REQUEST = 8


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


def _is_blocked_address(address: str) -> bool:
    """Allow only globally reachable addresses (RFC 6890)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        # An embedded v4 address would otherwise smuggle a rejected target past
        # the v6 checks.
        for embedded in (ip.ipv4_mapped, ip.sixtofour):
            if embedded is not None:
                return _is_blocked_address(str(embedded))
    return not ip.is_global or ip.is_multicast


async def _validated_fetch_target(url: str) -> tuple[str, str]:
    """Return the URL to request and its ``Host`` header.

    Cost estimation runs on the unauthenticated request body, so a caller can
    otherwise aim the node at internal hosts. HTTP is rewritten to the resolved
    address so the name cannot rebind between check and connect; HTTPS keeps its
    hostname because certificate validation already binds the connection.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parts.scheme or 'none'}")
    host = parts.hostname
    if not host:
        raise ValueError("missing host")

    default_port = 443 if parts.scheme == "https" else 80
    port = parts.port or default_port
    host_header = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        host_header = f"{host_header}:{parts.port}"

    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, proto=socket.IPPROTO_TCP
    )
    if not infos:
        raise ValueError("host did not resolve")
    for info in infos:
        if _is_blocked_address(str(info[4][0])):
            raise ValueError("host resolves to a blocked address")

    if parts.scheme == "https":
        return url, host_header

    family, _, _, _, sockaddr = infos[0]
    address = str(sockaddr[0])
    pinned = f"[{address}]" if family == socket.AF_INET6 else address
    if parts.port is not None:
        pinned = f"{pinned}:{parts.port}"
    return urlunsplit((parts.scheme, pinned, parts.path, parts.query, "")), host_header


async def _fetch_image_from_url(url: str) -> bytes | None:
    """Fetch the leading bytes of an image, enough to read its dimensions."""
    try:
        target, host_header = await _validated_fetch_target(url)
        async with httpx.AsyncClient(
            timeout=IMAGE_FETCH_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            async with client.stream(
                "GET", target, headers={"Host": host_header}
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                downloaded = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if downloaded >= IMAGE_FETCH_MAX_BYTES:
                        break
                return b"".join(chunks)[:IMAGE_FETCH_MAX_BYTES]
    except Exception as e:
        logger.warning(
            "Failed to fetch image from URL",
            extra={"error": str(e), "url": url[:100]},
        )
        return None


# Patch-based image pricing (OpenAI ``detail: "original"``): the image is
# covered with 32x32px patches and billed as ceil(patches * multiplier)
# tokens, with no 512px-tile downscaling. The API rejects images above
# 30,000 patches, so at the 1.2x multiplier documented for the
# original-capable model families (gpt-5.4/5.5/5.6) the worst case a
# single image can bill is 36,000 tokens.
_IMAGE_PATCH_PX = 32
_MAX_IMAGE_PATCHES = 30_000
_MAX_ORIGINAL_IMAGE_TOKENS = (_MAX_IMAGE_PATCHES * 6 + 4) // 5  # 36,000


def _calculate_original_image_tokens(width: int, height: int) -> int:
    """Estimate tokens for an image billed at ``detail: "original"``.

    Patch-based models cover the image with 32x32px patches and bill
    ``ceil(patches * 1.2)`` tokens. The estimate is bounded by the
    30,000-patch rejection limit, which is more conservative than the
    per-model resizing patch budgets (e.g. 10,000 patches on gpt-5.4/5.5)
    so it never under-reserves.
    """
    patches = ((width + _IMAGE_PATCH_PX - 1) // _IMAGE_PATCH_PX) * (
        (height + _IMAGE_PATCH_PX - 1) // _IMAGE_PATCH_PX
    )
    bounded = min(patches, _MAX_IMAGE_PATCHES)
    return (bounded * 6 + 4) // 5  # ceil(bounded * 1.2) in exact integer math


def _calculate_image_tokens(width: int, height: int, detail: str = "auto") -> int:
    """Calculate image tokens based on OpenAI's vision pricing.

    For low detail: 85 tokens
    For high detail/auto: 85 base tokens + 170 tokens per 512px tile
    For original detail: patch-based pricing at the original resolution
    """
    if detail == "low":
        return 85

    if detail == "original":
        return _calculate_original_image_tokens(width, height)

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
    fetches = 0

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

            # Responses-style ``input_image`` parts carry their detail and
            # file_id as siblings of the image reference; route them through
            # the input_image estimator so original detail / file_id are
            # honored on the chat path too.
            if content_type == "input_image":
                total_image_tokens += await _estimate_input_image_tokens(
                    content_item
                )
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
            elif fetches >= IMAGE_FETCH_MAX_PER_REQUEST:
                logger.warning(
                    "Skipping image URL fetch above per-request limit",
                    extra={"url": url[:100], "limit": IMAGE_FETCH_MAX_PER_REQUEST},
                )
                total_image_tokens += 85
            else:
                fetches += 1
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


async def _estimate_input_image_tokens(item: dict) -> int:
    """Estimate tokens for a Responses API ``input_image`` item.

    Honors the item-level ``detail``. The dimensions of ``file_id``
    references can't be fetched here, so they get conservative
    estimates: the max-size tile math for high/auto and the 30,000-patch
    worst case (36,000 tokens) for original, so we never under-reserve.
    """
    detail = item.get("detail") or "auto"
    if image_url := item.get("image_url"):
        if isinstance(image_url, dict):
            image_url = image_url.get("url", "")
        if isinstance(image_url, str) and image_url.startswith("data:image/"):
            try:
                _, base64_data = image_url.split(",", 1)
                image_bytes = base64.b64decode(base64_data)
                width, height = _get_image_dimensions(image_bytes)
                return _calculate_image_tokens(width, height, detail)
            except Exception as e:
                logger.warning(
                    "Failed to process base64 image", extra={"error": str(e)}
                )
                return 85
        # Remote URLs and file_id both have unfetchable dimensions here; fall
        # through to the conservative estimates below.
    if item.get("file_id") or item.get("image_url"):
        if detail == "original":
            return _MAX_ORIGINAL_IMAGE_TOKENS
        # We can't fetch an uploaded file's dimensions here; assume the
        # largest vision image so we don't under-reserve.
        return _calculate_image_tokens(2048, 2048, detail)
    return 0


async def estimate_image_tokens_from_input(input_data: Any) -> int:
    """Estimate total tokens for images embedded in a Responses API ``input``.

    Recognizes ``input_image`` items at the top level of the input list and
    inside ``message`` content parts.
    """
    if not isinstance(input_data, list):
        return 0

    total_image_tokens = 0
    for item in input_data:
        if not isinstance(item, dict):
            continue

        if item.get("type") == "input_image":
            total_image_tokens += await _estimate_input_image_tokens(item)
            continue

        content = item.get("content")
        if not isinstance(content, list):
            continue

        for part in content:
            if isinstance(part, dict) and part.get("type") == "input_image":
                total_image_tokens += await _estimate_input_image_tokens(part)

    return total_image_tokens


def create_error_response(
    error_type: str,
    message: str,
    status_code: int,
    request: Request,
    token: str | None = None,
    code: str | int | None = None,
    details: dict[str, object] | None = None,
) -> Response:
    """Create a standardized error response.

    ``code`` is a stable, machine-readable classification (e.g.
    ``UPSTREAM_RATE_LIMIT``); when omitted it defaults to the HTTP status code
    for backwards compatibility. ``details`` carries optional structured,
    redaction-safe context.
    """
    error_obj: dict[str, object] = {
        "message": redact_org_ids(message),
        "type": error_type,
        "code": code if code is not None else status_code,
    }
    if details is not None:
        error_obj["details"] = details
    return Response(
        content=json.dumps(
            {
                "error": error_obj,
                "request_id": getattr(request.state, "request_id", "unknown"),
            }
        ),
        status_code=status_code,
        media_type="application/json",
        headers={"X-Cashu": token} if token else {},
    )


def create_upstream_error_response(
    error: UpstreamError,
    request: Request,
    fallback_status: int = 502,
) -> Response:
    """Build an error response from an :class:`UpstreamError`, preserving its
    structured ``code``, ``details``, and original ``status_code``."""
    return create_error_response(
        "upstream_error",
        str(error),
        error.status_code or fallback_status,
        request=request,
        code=getattr(error, "code", None),
        details=getattr(error, "details", None),
    )
