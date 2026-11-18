"""Canonical, additive payment-required error details."""

from typing import Any


def build_payment_required_error(
    *,
    code: str,
    message: str,
    required_msats: int,
    balance_msats: int,
    reserved_msats: int = 0,
    model: str | None = None,
    pricing: dict[str, Any] | None = None,
    error_type: str = "insufficient_quota",
) -> dict[str, Any]:
    available = max(0, balance_msats - reserved_msats)
    details: dict[str, Any] = {
        "unit": "msat",
        "required_msats": required_msats,
        "available_msats": available,
        "shortfall_msats": max(0, required_msats - available),
        "pricing": pricing
        if pricing is not None
        else {
            "base_source": "unavailable",
            "formula": "unavailable",
            "required_msats": required_msats,
            "error": "Request pricing context was not supplied to this balance check",
        },
    }
    if reserved_msats:
        details["reserved_msats"] = reserved_msats
    if model is not None:
        details["model"] = model
    return {
        "error": {
            "type": error_type,
            "code": code,
            "message": message,
            "details": details,
        }
    }
