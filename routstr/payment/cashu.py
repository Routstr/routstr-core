from cashu.wallet.helpers import deserialize_token_from_string
from fastapi import HTTPException

from ..core import get_logger

logger = get_logger(__name__)


def get_token_balance_msat(cashu_token: str) -> int:
    try:
        token = deserialize_token_from_string(cashu_token)
    except Exception:
        raise HTTPException(401, "Invalid authentication token format")

    if token.unit == "sat":
        return token.amount * 1000
    if token.unit == "msat":
        return token.amount

    raise HTTPException(401, f"Unit {token.unit} not supported yet")


def pre_check_header_token_balance(
    headers: dict, body: dict, max_cost_for_model: int
) -> None:
    if x_cashu := headers.get("x-cashu", None):
        cashu_token = x_cashu
    elif auth := headers.get("authorization", None):
        cashu_token = auth.split(" ")[1] if len(auth.split(" ")) > 1 else ""
    else:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Handle regular API keys (sk-*)
    if cashu_token.startswith("sk-"):
        return

    amount_msat = get_token_balance_msat(cashu_token)

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
