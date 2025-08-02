import base64
import json
import os
from typing import Literal

import cbor2
from fastapi import HTTPException, Response

from ..core import get_logger
from .cost_caculation import COST_PER_REQUEST, MODEL_BASED_PRICING
from .models import MODELS

logger = get_logger(__name__)

UPSTREAM_BASE_URL = os.environ["UPSTREAM_BASE_URL"]
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")

logger.info(
    "Payment helpers initialized",
    extra={
        "upstream_base_url": UPSTREAM_BASE_URL,
        "has_upstream_api_key": bool(UPSTREAM_API_KEY),
        "model_based_pricing": MODEL_BASED_PRICING,
    },
)


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


def check_token_balance(headers: dict, body: dict) -> Literal["sat", "msat"]:
    """Check if the provided token has sufficient balance."""
    logger.debug(
        "Checking token balance",
        extra={
            "has_x_cashu": "x-cashu" in headers,
            "has_authorization": "authorization" in headers,
            "model": body.get("model", "unknown"),
        },
    )

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
        logger.debug(
            "Regular API key detected", extra={"key_preview": cashu_token[:10] + "..."}
        )
        return "sat"

    cost = get_cost_per_request(model=body.get("model", None))

    if cashu_token.startswith("cashuA"):
        logger.debug("Processing CashuA token", extra={"required_cost_msats": cost})

        try:
            _token = base64_token_json(cashu_token)
            amount = sum(p["amount"] for t in _token["token"] for p in t["proofs"])
            unit: Literal["sat", "msat"] = _token.get("unit", "sat")

            if unit == "sat":
                amount *= 1000

            logger.info(
                "CashuA token parsed successfully",
                extra={
                    "amount": amount,
                    "unit": unit,
                    "amount_msats": amount,
                    "required_cost_msats": cost,
                    "sufficient_balance": amount >= cost,
                },
            )

            if amount < cost:
                logger.warning(
                    "Insufficient token balance",
                    extra={
                        "amount_msats": amount,
                        "required_msats": cost,
                        "shortfall_msats": cost - amount,
                        "unit": unit,
                    },
                )
                raise HTTPException(status_code=413, detail="Insufficient balance")

        except Exception as e:
            logger.error(
                "Failed to parse CashuA token",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "token_preview": cashu_token[:20] + "...",
                },
            )
            raise HTTPException(status_code=401, detail="Invalid token format")

    elif cashu_token.startswith("cashuB"):
        logger.debug("Processing CashuB token", extra={"required_cost_msats": cost})

        try:
            _token = base64_token_cbor(cashu_token)
            amount = sum(p["a"] for t in _token["t"] for p in t["p"])
            unit = _token["u"]

            if unit == "sat":
                amount *= 1000

            logger.info(
                "CashuB token parsed successfully",
                extra={
                    "amount": amount,
                    "unit": unit,
                    "amount_msats": amount,
                    "required_cost_msats": cost,
                    "sufficient_balance": amount >= cost,
                },
            )

            if amount < cost:
                logger.warning(
                    "Insufficient token balance",
                    extra={
                        "amount_msats": amount,
                        "required_msats": cost,
                        "shortfall_msats": cost - amount,
                        "unit": unit,
                    },
                )
                raise HTTPException(status_code=413, detail="Insufficient balance")

        except Exception as e:
            logger.error(
                "Failed to parse CashuB token",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "token_preview": cashu_token[:20] + "...",
                },
            )
            raise HTTPException(status_code=401, detail="Invalid token format")

    else:
        logger.error(
            "Unknown token format",
            extra={"token_prefix": cashu_token[:10] if cashu_token else "empty"},
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    return unit


def base64_token_json(cashu_token: str) -> dict:
    """Decode a CashuA (JSON) token."""
    logger.debug("Decoding CashuA token", extra={"token_length": len(cashu_token)})

    try:
        # Version 3 - JSON format
        encoded = cashu_token[6:]  # Remove "cashuA"
        # Add correct padding – (-len) % 4 equals 0,1,2,3
        encoded += "=" * ((-len(encoded)) % 4)

        decoded = base64.urlsafe_b64decode(encoded).decode()
        token_data = json.loads(decoded)

        logger.debug(
            "CashuA token decoded successfully",
            extra={
                "token_proofs_count": sum(
                    len(t.get("proofs", [])) for t in token_data.get("token", [])
                ),
                "unit": token_data.get("unit", "unknown"),
            },
        )

        return token_data
    except Exception as e:
        logger.error(
            "Failed to decode CashuA token",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise


def base64_token_cbor(cashu_token: str) -> dict:
    """Decode a CashuB (CBOR) token."""
    logger.debug("Decoding CashuB token", extra={"token_length": len(cashu_token)})

    try:
        encoded = cashu_token[6:]  # Remove "cashuB"
        encoded += "=" * ((-len(encoded)) % 4)
        decoded_bytes = base64.urlsafe_b64decode(encoded)
        token_data = cbor2.loads(decoded_bytes)

        logger.debug(
            "CashuB token decoded successfully",
            extra={
                "token_proofs_count": sum(
                    len(t.get("p", [])) for t in token_data.get("t", [])
                ),
                "unit": token_data.get("u", "unknown"),
            },
        )

        return token_data
    except Exception as e:
        logger.error(
            "Failed to decode CashuB token",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise


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
