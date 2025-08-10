import json
import os
from typing import Optional

from fastapi import HTTPException, Response

from ..core.logging import get_logger
from ..core.settings import SettingsManager
from ..wallet import deserialize_token_from_string
from .cost_caculation import _get_cost_per_request, _is_model_based_pricing
from .models import MODELS

logger = get_logger(__name__)

# Cached settings - will be loaded from database
_UPSTREAM_BASE_URL = None
_UPSTREAM_API_KEY = None


async def _get_upstream_base_url() -> str:
    """Get upstream base URL from settings."""
    global _UPSTREAM_BASE_URL
    if _UPSTREAM_BASE_URL is None:
        _UPSTREAM_BASE_URL = await SettingsManager.get("UPSTREAM_BASE_URL", "")
        if not _UPSTREAM_BASE_URL:
            raise ValueError(
                "UPSTREAM_BASE_URL setting is required but not set. "
                "Please set it to your API provider's base URL (e.g., https://api.openai.com/v1)"
            )
    return _UPSTREAM_BASE_URL


async def _get_upstream_api_key() -> str:
    """Get upstream API key from settings."""
    global _UPSTREAM_API_KEY
    if _UPSTREAM_API_KEY is None:
        _UPSTREAM_API_KEY = await SettingsManager.get("UPSTREAM_API_KEY", "")
        if not _UPSTREAM_API_KEY:
            raise ValueError(
                "UPSTREAM_API_KEY setting is required but not set. "
                "Please set it to your API provider's API key"
            )
    return _UPSTREAM_API_KEY


# Cache reload function
async def reload_upstream_settings() -> None:
    """Reload upstream settings from database."""
    global _UPSTREAM_BASE_URL, _UPSTREAM_API_KEY
    _UPSTREAM_BASE_URL = None
    _UPSTREAM_API_KEY = None


# For backward compatibility during initialization
UPSTREAM_BASE_URL = os.environ.get("UPSTREAM_BASE_URL", "")
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")

if not UPSTREAM_BASE_URL:
    logger.warning(
        "UPSTREAM_BASE_URL environment variable not set. "
        "It will need to be configured in settings."
    )

if not UPSTREAM_API_KEY:
    logger.warning(
        "UPSTREAM_API_KEY environment variable not set. "
        "It will need to be configured in settings."
    )


async def get_cost_per_request(model: str | None = None) -> int:
    """Get the cost per request for a given model."""
    logger.debug(
        "Calculating cost per request",
        extra={
            "model": model,
            "model_based_pricing": await _is_model_based_pricing(),
            "has_models": bool(MODELS),
        },
    )

    if await _is_model_based_pricing() and MODELS and model:
        cost = await get_max_cost_for_model(model=model)
        logger.debug(
            "Using model-based cost", extra={"model": model, "cost_msats": cost}
        )
        return cost

    logger.debug(
        "Using default cost per request",
        extra={"cost_msats": await _get_cost_per_request()},
    )
    return await _get_cost_per_request()


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


async def get_max_cost_for_model(model: str) -> int:
    """
    Get the maximum cost for a model based on its pricing configuration.

    Args:
        model: The model identifier

    Returns:
        Maximum cost in millisats
    """
    if not await _is_model_based_pricing() or not MODELS:
        logger.debug(
            "Using default cost (no model-based pricing)",
            extra={"cost_msats": await _get_cost_per_request(), "model": model},
        )
        return await _get_cost_per_request()

    if model not in [model.id for model in MODELS]:
        logger.warning(
            "Unknown model requested, using default cost",
            extra={
                "requested_model": model,
                "available_models": [m.id for m in MODELS],
                "using_default_cost": await _get_cost_per_request(),
            },
        )
        return await _get_cost_per_request()

    for m in MODELS:
        if m.id == model:
            # Check if sats_pricing exists and has max_cost
            if m.sats_pricing and hasattr(m.sats_pricing, "max_cost"):
                cost = int(m.sats_pricing.max_cost * 1000)  # Convert sats to msats
            else:
                cost = await _get_cost_per_request()
            logger.debug(
                "Found model pricing",
                extra={"model": model, "cost_msats": cost},
            )
            return cost

    logger.warning(
        "Model pricing not found, using default",
        extra={"model": model, "default_cost_msats": await _get_cost_per_request()},
    )
    return await _get_cost_per_request()


def create_error_response(
    error_type: str, message: str, status_code: int, token: Optional[str] = None
) -> Response:
    """Create a standardized error response."""
    logger.info(
        "Creating error response",
        extra={
            "error_type": error_type,
            "error_message": message,
            "status_code": status_code,
        },
    )

    response_headers = {}
    if token:
        response_headers["X-Cashu"] = token
    return Response(
        content=json.dumps(
            {
                "error": {
                    "message": message,
                    "type": error_type,
                    "code": status_code,
                }
            }
        ),
        status_code=status_code,
        media_type="application/json",
        headers=dict(response_headers),
    )


def prepare_upstream_headers(request_headers: dict) -> dict:
    """Prepare headers for upstream request, removing sensitive/problematic ones."""
    logger.debug(
        "Preparing upstream headers",
        extra={
            "original_headers_count": len(request_headers),
            "has_upstream_api_key": bool(UPSTREAM_API_KEY),
        },
    )

    headers = dict(request_headers)

    # Remove headers that shouldn't be forwarded
    removed_headers = []
    for header in [
        "host",
        "content-length",
        "refund-lnurl",
        "key-expiry-time",
        "x-cashu",
    ]:
        if headers.pop(header, None) is not None:
            removed_headers.append(header)

    # Handle authorization
    if UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
        if headers.pop("authorization", None) is not None:
            removed_headers.append("authorization (replaced with upstream key)")
    else:
        for auth_header in ["Authorization", "authorization"]:
            if headers.pop(auth_header, None) is not None:
                removed_headers.append(auth_header)

    logger.debug(
        "Headers prepared for upstream",
        extra={
            "final_headers_count": len(headers),
            "removed_headers": removed_headers,
            "added_upstream_auth": bool(UPSTREAM_API_KEY),
        },
    )

    return headers
