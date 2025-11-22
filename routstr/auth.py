import hashlib
import math
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, update

from .core import get_logger
from .core.db import ApiKey, AsyncSession
from .core.settings import settings
from .payment.cost_caculation import (
    CostData,
    CostDataError,
    MaxCostData,
    calculate_cost,
)
from .wallet import credit_balance, deserialize_token_from_string

logger = get_logger(__name__)

# TODO: implement prepaid api key (not like it was before)
# PREPAID_API_KEY = os.environ.get("PREPAID_API_KEY", None)
# PREPAID_BALANCE = int(os.environ.get("PREPAID_BALANCE", "0")) * 1000  # Convert to msats


async def validate_bearer_key(
    bearer_key: str,
    session: AsyncSession,
    refund_address: Optional[str] = None,
    key_expiry_time: Optional[int] = None,
) -> ApiKey:
    """
    Validates the provided API key using SQLModel.
    If it's a cashu key, it redeems it and stores its hash and balance.
    Otherwise checks if the hash of the key exists.
    """
    logger.debug(
        "Starting bearer key validation",
        extra={
            "key_preview": bearer_key[:20] + "..."
            if len(bearer_key) > 20
            else bearer_key,
            "has_refund_address": bool(refund_address),
            "has_expiry_time": bool(key_expiry_time),
        },
    )

    if not bearer_key:
        logger.error("Empty bearer key provided")
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

    if bearer_key.startswith("sk-"):
        logger.debug(
            "Processing sk- prefixed API key",
            extra={"key_preview": bearer_key[:10] + "..."},
        )

        if existing_key := await session.get(ApiKey, bearer_key[3:]):
            logger.info(
                "Existing sk- API key found",
                extra={
                    "key_hash": existing_key.hashed_key[:8] + "...",
                    "balance": existing_key.balance,
                    "total_requests": existing_key.total_requests,
                },
            )

            if key_expiry_time is not None:
                existing_key.key_expiry_time = key_expiry_time
                logger.debug(
                    "Updated key expiry time",
                    extra={
                        "key_hash": existing_key.hashed_key[:8] + "...",
                        "expiry_time": key_expiry_time,
                    },
                )

            if refund_address is not None:
                existing_key.refund_address = refund_address
                logger.debug(
                    "Updated refund address",
                    extra={
                        "key_hash": existing_key.hashed_key[:8] + "...",
                        "refund_address_preview": refund_address[:20] + "..."
                        if len(refund_address) > 20
                        else refund_address,
                    },
                )

            return existing_key
        else:
            logger.warning(
                "sk- API key not found in database",
                extra={"key_preview": bearer_key[:10] + "..."},
            )

    if bearer_key.startswith("cashu"):
        logger.debug(
            "Processing Cashu token",
            extra={
                "token_preview": bearer_key[:20] + "...",
                "token_type": bearer_key[:6] if len(bearer_key) >= 6 else bearer_key,
            },
        )

        try:
            hashed_key = hashlib.sha256(bearer_key.encode()).hexdigest()
            token_obj = deserialize_token_from_string(bearer_key)
            logger.debug(
                "Generated token hash", extra={"hash_preview": hashed_key[:16] + "..."}
            )

            if existing_key := await session.get(ApiKey, hashed_key):
                logger.info(
                    "Existing Cashu token found",
                    extra={
                        "key_hash": existing_key.hashed_key[:8] + "...",
                        "balance": existing_key.balance,
                        "total_requests": existing_key.total_requests,
                    },
                )

                if key_expiry_time is not None:
                    existing_key.key_expiry_time = key_expiry_time
                    logger.debug(
                        "Updated key expiry time for existing Cashu key",
                        extra={
                            "key_hash": existing_key.hashed_key[:8] + "...",
                            "expiry_time": key_expiry_time,
                        },
                    )

                if refund_address is not None:
                    existing_key.refund_address = refund_address
                    logger.debug(
                        "Updated refund address for existing Cashu key",
                        extra={
                            "key_hash": existing_key.hashed_key[:8] + "...",
                            "refund_address_preview": refund_address[:20] + "..."
                            if len(refund_address) > 20
                            else refund_address,
                        },
                    )

                return existing_key

            logger.info(
                "Creating new Cashu token entry",
                extra={
                    "hash_preview": hashed_key[:16] + "...",
                    "has_refund_address": bool(refund_address),
                    "has_expiry_time": bool(key_expiry_time),
                },
            )
            if token_obj.mint in settings.cashu_mints:
                refund_currency = token_obj.unit
                refund_mint_url = token_obj.mint
            else:
                refund_currency = "sat"
                refund_mint_url = settings.primary_mint

            new_key = ApiKey(
                hashed_key=hashed_key,
                balance=0,
                refund_address=refund_address,
                key_expiry_time=key_expiry_time,
                refund_currency=refund_currency,
                refund_mint_url=refund_mint_url,
            )
            session.add(new_key)

            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "Concurrent key creation detected, fetching existing key",
                    extra={"key_hash": hashed_key[:8] + "..."},
                )
                existing_key = await session.get(ApiKey, hashed_key)
                if not existing_key:
                    raise Exception("Failed to fetch existing key after IntegrityError")

                if key_expiry_time is not None:
                    existing_key.key_expiry_time = key_expiry_time
                if refund_address is not None:
                    existing_key.refund_address = refund_address

                return existing_key

            logger.debug(
                "New key created, starting token redemption",
                extra={"key_hash": hashed_key[:8] + "..."},
            )

            logger.info(
                "AUTH: About to call credit_balance",
                extra={"token_preview": bearer_key[:50]},
            )
            try:
                msats = await credit_balance(bearer_key, new_key, session)
                logger.info(
                    "AUTH: credit_balance returned successfully", extra={"msats": msats}
                )
            except Exception as credit_error:
                logger.error(
                    "AUTH: credit_balance failed",
                    extra={
                        "error": str(credit_error),
                        "error_type": type(credit_error).__name__,
                    },
                )
                raise credit_error

            if msats <= 0:
                logger.error(
                    "Token redemption returned zero or negative amount",
                    extra={"msats": msats, "key_hash": hashed_key[:8] + "..."},
                )
                raise Exception("Token redemption failed")

            await session.refresh(new_key)
            await session.commit()

            logger.info(
                "New Cashu token successfully redeemed and stored",
                extra={
                    "key_hash": hashed_key[:8] + "...",
                    "redeemed_msats": msats,
                    "final_balance": new_key.balance,
                },
            )

            return new_key
        except Exception as e:
            logger.error(
                "Cashu token redemption failed",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "token_preview": bearer_key[:20] + "..."
                    if len(bearer_key) > 20
                    else bearer_key,
                },
            )
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": f"Invalid or expired Cashu key: {str(e)}",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )

    logger.error(
        "Invalid API key format",
        extra={
            "key_preview": bearer_key[:10] + "..."
            if len(bearer_key) > 10
            else bearer_key,
            "key_length": len(bearer_key),
        },
    )

    raise HTTPException(
        status_code=401,
        detail={
            "error": {
                "message": "Invalid API key",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )


async def pay_for_request(
    key: ApiKey, cost_per_request: int, session: AsyncSession
) -> int:
    """Process payment for a request."""

    logger.info(
        "Processing payment for request",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "current_balance": key.balance,
            "required_cost": cost_per_request,
            "sufficient_balance": key.balance >= cost_per_request,
        },
    )

    if key.total_balance < cost_per_request:
        logger.warning(
            "Insufficient balance for request",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "balance": key.balance,
                "reserved_balance": key.reserved_balance,
                "required": cost_per_request,
                "shortfall": cost_per_request - key.total_balance,
            },
        )

        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": f"Insufficient balance: {cost_per_request} mSats required. {key.total_balance} available. (reserved: {key.reserved_balance})",
                    "type": "insufficient_quota",
                    "code": "insufficient_balance",
                }
            },
        )

    logger.debug(
        "Charging base cost for request",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "cost": cost_per_request,
            "balance_before": key.balance,
        },
    )

    # Charge the base cost for the request atomically to avoid race conditions
    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key.hashed_key)
        .where(col(ApiKey.balance) >= cost_per_request)
        .values(
            reserved_balance=col(ApiKey.reserved_balance) + cost_per_request,
            total_requests=col(ApiKey.total_requests) + 1,
        )
    )
    result = await session.exec(stmt)  # type: ignore[call-overload]
    await session.commit()

    if result.rowcount == 0:
        logger.error(
            "Concurrent request depleted balance",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "required_cost": cost_per_request,
                "current_balance": key.balance,
            },
        )

        # Another concurrent request spent the balance first
        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": f"Insufficient balance: {cost_per_request} mSats required. {key.balance} available.",
                    "type": "insufficient_quota",
                    "code": "insufficient_balance",
                }
            },
        )

    await session.refresh(key)

    # IMPORTANT: Do not modify this log string; usage analytics relies on it.
    logger.info(
        "Payment processed successfully",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "charged_amount": cost_per_request,
            "new_balance": key.balance,
            "total_spent": key.total_spent,
            "total_requests": key.total_requests,
        },
    )

    return cost_per_request


async def revert_pay_for_request(
    key: ApiKey, session: AsyncSession, cost_per_request: int
) -> None:
    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key.hashed_key)
        .values(
            reserved_balance=col(ApiKey.reserved_balance) - cost_per_request,
            total_requests=col(ApiKey.total_requests) - 1,
        )
    )

    result = await session.exec(stmt)  # type: ignore[call-overload]
    await session.commit()
    if result.rowcount == 0:
        # IMPORTANT: Do not modify this log string; analytics rely on it to flag refund failures.
        logger.error(
            "Failed to revert payment - insufficient reserved balance",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "cost_to_revert": cost_per_request,
                "current_reserved_balance": key.reserved_balance,
            },
        )
        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": f"failed to revert request payment: {cost_per_request} mSats required. {key.balance} available.",
                    "type": "payment_error",
                    "code": "payment_error",
                }
            },
        )
    await session.refresh(key)


async def adjust_payment_for_tokens(
    key: ApiKey, response_data: dict, session: AsyncSession, deducted_max_cost: int
) -> dict:
    """
    Adjusts the payment based on token usage in the response.
    This is called after the initial payment and the upstream request is complete.
    Returns cost data to be included in the response.
    """
    model = response_data.get("model", "unknown")

    logger.debug(
        "Starting payment adjustment for tokens",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "model": model,
            "deducted_max_cost": deducted_max_cost,
            "current_balance": key.balance,
            "has_usage": "usage" in response_data,
        },
    )

    match await calculate_cost(response_data, deducted_max_cost, session):
        case MaxCostData() as cost:
            logger.debug(
                "Using max cost data (no token adjustment)",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "model": model,
                    "max_cost": cost.total_msats,
                },
            )
            # Finalize by releasing reservation and charging max cost
            finalize_stmt = (
                update(ApiKey)
                .where(col(ApiKey.hashed_key) == key.hashed_key)
                .values(
                    reserved_balance=col(ApiKey.reserved_balance) - deducted_max_cost,
                    balance=col(ApiKey.balance) - cost.total_msats,
                    total_spent=col(ApiKey.total_spent) + cost.total_msats,
                )
            )
            result = await session.exec(finalize_stmt)  # type: ignore[call-overload]
            await session.commit()
            if result.rowcount == 0:
                logger.error(
                    "Failed to finalize max-cost payment - insufficient reserved balance",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "deducted_max_cost": deducted_max_cost,
                        "current_reserved_balance": key.reserved_balance,
                        "total_cost": cost.total_msats,
                        "model": model,
                    },
                )
            else:
                await session.refresh(key)
                logger.info(
                    "Max cost payment finalized",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "charged_amount": cost.total_msats,
                        "new_balance": key.balance,
                        "model": model,
                    },
                )
            return cost.dict()

        case CostData() as cost:
            # If token-based pricing is enabled and base cost is 0, use token-based cost
            # Otherwise, token cost is additional to the base cost
            cost_difference = cost.total_msats - deducted_max_cost
            total_cost_msats: int = math.ceil(cost.total_msats)

            logger.info(
                "Calculated token-based cost",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "model": model,
                    "token_cost": cost.total_msats,
                    "deducted_max_cost": deducted_max_cost,
                    "cost_difference": cost_difference,
                    "input_msats": cost.input_msats,
                    "output_msats": cost.output_msats,
                },
            )

            if cost_difference == 0:
                logger.debug(
                    "Finalizing with exact reserved cost",
                    extra={"key_hash": key.hashed_key[:8] + "...", "model": model},
                )
                finalize_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == key.hashed_key)
                    .values(
                        reserved_balance=col(ApiKey.reserved_balance)
                        - deducted_max_cost,
                        balance=col(ApiKey.balance) - total_cost_msats,
                        total_spent=col(ApiKey.total_spent) + total_cost_msats,
                    )
                )
                await session.exec(finalize_stmt)  # type: ignore[call-overload]
                await session.commit()
                await session.refresh(key)
                return cost.dict()

            # this should never happen why do we handle this???
            if cost_difference > 0:
                # Need to charge more than reserved, finalize by releasing reservation and charging total
                logger.info(
                    "Additional charge required for token usage",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "additional_charge": cost_difference,
                        "current_balance": key.balance,
                        "sufficient_balance": key.balance >= cost_difference,
                        "model": model,
                    },
                )

                finalize_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == key.hashed_key)
                    .values(
                        reserved_balance=col(ApiKey.reserved_balance)
                        - deducted_max_cost,
                        balance=col(ApiKey.balance) - total_cost_msats,
                        total_spent=col(ApiKey.total_spent) + total_cost_msats,
                    )
                )
                result = await session.exec(finalize_stmt)  # type: ignore[call-overload]
                await session.commit()

                if result.rowcount:
                    cost.total_msats = total_cost_msats
                    await session.refresh(key)

                    logger.info(
                        "Finalized payment with additional charge",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "charged_amount": total_cost_msats,
                            "new_balance": key.balance,
                            "model": model,
                        },
                    )
                else:
                    logger.warning(
                        "Failed to finalize additional charge (concurrent operation)",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "attempted_charge": total_cost_msats,
                            "model": model,
                        },
                    )
            else:
                # Refund some of the base cost
                refund = abs(cost_difference)
                logger.info(
                    "Refunding excess payment",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "refund_amount": refund,
                        "current_balance": key.balance,
                        "model": model,
                    },
                )

                refund_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == key.hashed_key)
                    .values(
                        reserved_balance=col(ApiKey.reserved_balance)
                        - deducted_max_cost,
                        balance=col(ApiKey.balance) - total_cost_msats,
                        total_spent=col(ApiKey.total_spent) + total_cost_msats,
                    )
                )
                result = await session.exec(refund_stmt)  # type: ignore[call-overload]
                await session.commit()

                if result.rowcount == 0:
                    logger.error(
                        "Failed to finalize payment - insufficient reserved balance",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "deducted_max_cost": deducted_max_cost,
                            "current_reserved_balance": key.reserved_balance,
                            "total_cost": total_cost_msats,
                            "model": model,
                        },
                    )
                    # Still return the cost data even if we couldn't properly finalize
                    # The reservation was already made, so the user has paid

                cost.total_msats = total_cost_msats
                await session.refresh(key)

                logger.info(
                    "Refund processed successfully",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "refunded_amount": refund,
                        "new_balance": key.balance,
                        "final_cost": cost.total_msats,
                        "model": model,
                    },
                )

            return cost.dict()

        case CostDataError() as error:
            logger.error(
                "Cost calculation error during payment adjustment",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "model": model,
                    "error_message": error.message,
                    "error_code": error.code,
                },
            )

            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": error.message,
                        "type": "invalid_request_error",
                        "code": error.code,
                    }
                },
            )
    # Fallback return to satisfy type checker; execution should not reach here
    return {
        "base_msats": deducted_max_cost,
        "input_msats": 0,
        "output_msats": 0,
        "total_msats": deducted_max_cost,
    }
