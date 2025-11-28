from cashu.wallet.helpers import deserialize_token_from_string
from fastapi import HTTPException

from ..core import get_logger

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
