import json
import os
from typing import Optional

from fastapi import HTTPException, Response

from ..core import get_logger
from ..wallet import deserialize_token_from_string
from .cost_caculation import COST_PER_REQUEST, MODEL_BASED_PRICING
from .models import MODELS, Model

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


def prepare_upstream_headers(
    request_headers: dict, model_name: str | None = None
) -> dict:
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

    # Handle authorization - check for model-specific API key first
    model_api_key = None
    if model_name:
        model = get_model_by_name(model_name)
        # Note: This would require the Model class to have an api_key field
        # For now, we'll just use the upstream API key
        # In a more complete implementation, you'd need to add api_key to the Model class

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


def extract_model_from_request(request_body: dict) -> str | None:
    """Extract model name from request body, similar to Rust OpenAIRequest."""
    model = request_body.get("model")
    if isinstance(model, str):
        logger.debug("Extracted model from request", extra={"model": model})
        return model

    logger.debug("No model found in request body")
    return None


def get_model_by_name(model_name: str) -> Model | None:
    """Get model from MODELS list by name."""
    if not MODELS or not model_name:
        logger.debug(
            "No models available or no model name provided",
            extra={"has_models": bool(MODELS), "model_name": model_name},
        )
        return None

    for model in MODELS:
        if model.id == model_name:
            logger.debug(
                "Found model in MODELS list",
                extra={
                    "model_id": model.id,
                    "model_name": model.name,
                    "has_provider_url": bool(model.provider_url),
                },
            )
            return model

    logger.debug(
        "Model not found in MODELS list",
        extra={
            "requested_model": model_name,
            "available_models": [m.id for m in MODELS],
        },
    )
    return None


def get_provider_url_for_model(model_name: str) -> str | None:
    """Get provider URL for a specific model."""
    model = get_model_by_name(model_name)
    if model and model.provider_url and model.provider_url.strip():
        logger.debug(
            "Found provider URL for model",
            extra={"model": model_name, "provider_url": model.provider_url},
        )
        return model.provider_url.strip()

    logger.debug(
        "No provider URL found for model, will use default",
        extra={
            "model": model_name,
            "model_found": model is not None,
            "provider_url": model.provider_url if model else None,
            "default_url": UPSTREAM_BASE_URL,
        },
    )
    return None


def get_upstream_url_for_request(path: str, request_body: dict | None = None) -> str:
    """
    Get the appropriate upstream URL for a request.
    Priority order:
    1. Model-specific provider_url (if model exists and has valid provider_url)
    2. Default UPSTREAM_BASE_URL (fallback for all other cases)

    Fallback scenarios:
    - No request body provided
    - No model specified in request body
    - Model not found in MODELS list
    - Model found but no provider_url set
    - Model found but provider_url is empty/whitespace
    """
    if path.startswith("v1/"):
        path = path.replace("v1/", "")

    # Try to extract model name and get provider URL
    model_name = None
    provider_url = None
    fallback_reason = "no_request_body"

    if request_body:
        model_name = extract_model_from_request(request_body)
        if model_name:
            provider_url = get_provider_url_for_model(model_name)
            if provider_url:
                # Remove trailing slash and construct full URL
                base_url = provider_url.rstrip("/")
                final_url = f"{base_url}/{path}"
                logger.info(
                    "Using model-specific provider URL",
                    extra={
                        "model": model_name,
                        "provider_url": provider_url,
                        "final_url": final_url,
                        "path": path,
                    },
                )
                return final_url
            else:
                fallback_reason = "no_provider_url"
        else:
            fallback_reason = "no_model_in_request"

    # Fallback to default upstream URL
    final_url = f"{UPSTREAM_BASE_URL}/{path}"
    logger.info(
        "Using default UPSTREAM_BASE_URL fallback",
        extra={
            "final_url": final_url,
            "path": path,
            "fallback_reason": fallback_reason,
            "model_name": model_name,
            "has_request_body": bool(request_body),
            "upstream_base_url": UPSTREAM_BASE_URL,
        },
    )
    return final_url
