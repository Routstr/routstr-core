import json
from typing import Mapping

from fastapi import HTTPException, Response
from fastapi.requests import Request

from ..core import get_logger
from ..core.settings import settings
from ..wallet import deserialize_token_from_string
from .models import MODELS

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


def get_max_cost_for_model(model: str, tolerance_percentage: int = 1) -> int:
    """Get the maximum cost for a specific model."""
    logger.debug(
        "Getting max cost for model",
        extra={
            "model": model,
            "fixed_pricing": settings.fixed_pricing,
            "has_models": bool(MODELS),
        },
    )

    # Fixed pricing: always use fixed_cost_per_request
    if settings.fixed_pricing:
        default_cost_msats = settings.fixed_cost_per_request * 1000
        logger.debug(
            "Using fixed cost pricing",
            extra={"cost_msats": default_cost_msats, "model": model},
        )
        return default_cost_msats

    if model not in [model.id for model in MODELS]:
        # If no models or unknown model, fall back to fixed cost if provided, else minimal default
        fallback_msats = settings.fixed_cost_per_request * 1000
        logger.warning(
            "Model not found in available models",
            extra={
                "requested_model": model,
                "available_models": [m.id for m in MODELS],
                "using_default_cost": fallback_msats,
            },
        )
        return fallback_msats

    for m in MODELS:
        if m.id == model:
            max_cost = m.sats_pricing.max_cost * 1000 * (1 - tolerance_percentage / 100)  # type: ignore
            logger.debug(
                "Found model-specific max cost",
                extra={"model": model, "max_cost_msats": max_cost},
            )
            return int(max_cost)

    logger.warning(
        "Model pricing not found, using fixed cost",
        extra={
            "model": model,
            "default_cost_msats": settings.fixed_cost_per_request * 1000,
        },
    )
    return settings.fixed_cost_per_request * 1000


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


def prepare_upstream_headers(request_headers: dict) -> dict:
    """Prepare headers for upstream request, removing sensitive/problematic ones."""
    upstream_api_key = settings.upstream_api_key
    logger.debug(
        "Preparing upstream headers",
        extra={
            "original_headers_count": len(request_headers),
            "has_upstream_api_key": bool(upstream_api_key),
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
    if upstream_api_key:
        headers["Authorization"] = f"Bearer {upstream_api_key}"
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
            "added_upstream_auth": bool(upstream_api_key),
        },
    )

    return headers


def prepare_upstream_params(
    path: str, query_params: Mapping[str, str] | None
) -> dict[str, str]:
    """Prepare query params for upstream request, optionally adding api-version for chat/completions."""
    params: dict[str, str] = dict(query_params or {})
    chat_api_version = settings.chat_completions_api_version
    if path.endswith("chat/completions") and chat_api_version:
        params["api-version"] = chat_api_version
    return params
