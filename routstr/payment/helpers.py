import json
import math
from typing import Any

from fastapi import HTTPException, Response
from fastapi.requests import Request
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
        from ..proxy import get_upstreams
        from ..upstream import get_model_with_override

        upstreams = get_upstreams()
        model_obj = await get_model_with_override(model, upstreams, session)

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
        estimated_prompt_delta_sats = (
            max_prompt_allowed_sats - prompt_tokens * model_pricing.prompt
        )
        if estimated_prompt_delta_sats >= 0:
            adjusted = adjusted - math.floor(estimated_prompt_delta_sats * 1000)
        else:
            adjusted = adjusted + math.ceil(-estimated_prompt_delta_sats * 1000)

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
            if estimated_completion_delta_sats >= 0:
                adjusted = adjusted - math.floor(estimated_completion_delta_sats * 1000)
            else:
                adjusted = adjusted + math.ceil(-estimated_completion_delta_sats * 1000)

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
    return len(str(messages)) // 3


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
