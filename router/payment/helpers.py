import json
import os

from fastapi import HTTPException, Response

from ..core import get_logger
from ..wallet import deserialize_token_from_string
from .cost_caculation import COST_PER_REQUEST, MODEL_BASED_PRICING
from .models import MODELS

logger = get_logger(__name__)


UPSTREAM_BASE_URL = os.environ.get("UPSTREAM_BASE_URL", "")
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")

if not UPSTREAM_BASE_URL:
    raise ValueError("Please set the UPSTREAM_BASE_URL environment variable")


def get_cost_per_request(model: str | None = None) -> int:
    """Get the cost per request for a given model."""
    logger.debug(
        "Calculating cost per request",
        extra={
            "model": model,
            "model_based_pricing": MODEL_BASED_PRICING,
            "has_models": bool(MODELS),
        },
    )

    if MODEL_BASED_PRICING and MODELS and model:
        cost = get_max_cost_for_model(model=model)
        logger.debug(
            "Using model-based cost", extra={"model": model, "cost_msats": cost}
        )
        return cost

    logger.debug(
        "Using default cost per request", extra={"cost_msats": COST_PER_REQUEST}
    )
    return COST_PER_REQUEST


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

    token_obj = deserialize_token_from_string(cashu_token)

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


def get_max_cost_for_model(model: str) -> int:
    """Get the maximum cost for a specific model."""
    logger.debug(
        "Getting max cost for model",
        extra={
            "model": model,
            "model_based_pricing": MODEL_BASED_PRICING,
            "has_models": bool(MODELS),
        },
    )

    if not MODEL_BASED_PRICING or not MODELS:
        logger.debug(
            "Using default cost (no model-based pricing)",
            extra={"cost_msats": COST_PER_REQUEST, "model": model},
        )
        return COST_PER_REQUEST

    if model not in [model.id for model in MODELS]:
        logger.warning(
            "Model not found in available models",
            extra={
                "requested_model": model,
                "available_models": [m.id for m in MODELS],
                "using_default_cost": COST_PER_REQUEST,
            },
        )
        return COST_PER_REQUEST

    for m in MODELS:
        if m.id == model:
            max_cost = m.sats_pricing.max_cost * 1000  # type: ignore
            logger.debug(
                "Found model-specific max cost",
                extra={"model": model, "max_cost_msats": max_cost},
            )
            return int(max_cost)

    logger.warning(
        "Model pricing not found, using default",
        extra={"model": model, "default_cost_msats": COST_PER_REQUEST},
    )
    return COST_PER_REQUEST


def create_error_response(error_type: str, message: str, status_code: int) -> Response:
    """Create a standardized error response."""
    logger.info(
        "Creating error response",
        extra={
            "error_type": error_type,
            "error_message": message,
            "status_code": status_code,
        },
    )

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
