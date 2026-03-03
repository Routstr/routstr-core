import asyncio
import hashlib
import math
import random
import time
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select, update

from .core import get_logger
from .core.db import ApiKey, AsyncSession
from .core.settings import settings
from .payment.cost_calculation import (
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


async def check_and_reset_limit(key: ApiKey, session: AsyncSession) -> bool:
    """Checks if a key's balance limit should be reset based on its policy."""
    if key.balance_limit is not None and key.balance_limit_reset:
        now = int(time.time())
        reset_date = key.balance_limit_reset_date or 0
        should_reset = False

        if key.balance_limit_reset == "daily":
            if (
                datetime.fromtimestamp(now).date()
                > datetime.fromtimestamp(reset_date).date()
            ):
                should_reset = True
        elif key.balance_limit_reset == "weekly":
            if (
                datetime.fromtimestamp(now).isocalendar()[:2]
                > datetime.fromtimestamp(reset_date).isocalendar()[:2]
            ):
                should_reset = True
        elif key.balance_limit_reset == "monthly":
            dt_now = datetime.fromtimestamp(now)
            dt_reset = datetime.fromtimestamp(reset_date)
            if dt_now.year > dt_reset.year or dt_now.month > dt_reset.month:
                should_reset = True

        if should_reset:
            logger.info(
                "Resetting balance limit for key",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "policy": key.balance_limit_reset,
                    "old_spent": key.total_spent,
                },
            )
            key.total_spent = 0
            key.balance_limit_reset_date = now
            session.add(key)
            await session.flush()
            return True
    return False


async def validate_bearer_key(
    bearer_key: str,
    session: AsyncSession,
    refund_address: Optional[str] = None,
    key_expiry_time: Optional[int] = None,
    min_cost: int = 0,
) -> ApiKey:
    """
    Validates the provided API key using SQLModel.
    If it's a cashu key, it redeems it and stores its hash and balance.
    Otherwise checks if the hash of the key exists.
    Includes a balance check against min_cost for limited keys.
    """
    logger.debug(
        "Starting bearer key validation",
        extra={
            "key_preview": bearer_key[:20] + "..."
            if len(bearer_key) > 20
            else bearer_key,
            "has_refund_address": bool(refund_address),
            "has_expiry_time": bool(key_expiry_time),
            "min_cost": min_cost,
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

            # Check and reset limit if needed
            await check_and_reset_limit(existing_key, session)

            # Early check: Billing balance check (Parent balance)
            billing_key = await get_billing_key(existing_key, session)
            if min_cost > 0 and billing_key.total_balance < min_cost:
                logger.warning(
                    "Insufficient billing balance during validation",
                    extra={
                        "key_hash": existing_key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "balance": billing_key.total_balance,
                        "required": min_cost,
                    },
                )
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": {
                            "message": f"Insufficient balance: {min_cost} mSats required for this model. {billing_key.total_balance} available.",
                            "type": "insufficient_quota",
                            "code": "insufficient_balance",
                        }
                    },
                )

            # Early check: Spending limit check (Child key limit)
            if (
                min_cost > 0
                and existing_key.balance_limit is not None
                and existing_key.total_spent + existing_key.reserved_balance + min_cost
                > existing_key.balance_limit
            ):
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": {
                            "message": f"Balance limit exceeded: {existing_key.balance_limit} mSats limit. {existing_key.total_spent} already spent ({existing_key.reserved_balance} reserved), {min_cost} minimum required for this model.",
                            "type": "insufficient_quota",
                            "code": "balance_limit_exceeded",
                        }
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

                # Early check: Billing balance check
                if min_cost > 0 and existing_key.total_balance < min_cost:
                    raise HTTPException(
                        status_code=402,
                        detail={
                            "error": {
                                "message": f"Insufficient balance: {min_cost} mSats required for this model. {existing_key.total_balance} available.",
                                "type": "insufficient_quota",
                                "code": "insufficient_balance",
                            }
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
        except HTTPException:
            raise
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


async def get_billing_key(key: ApiKey, session: AsyncSession) -> ApiKey:
    """Returns the key that should be charged for the request."""
    if key.parent_key_hash:
        parent = await session.get(ApiKey, key.parent_key_hash)
        if parent:
            # We want to keep the total_requests and total_spent on the child key
            # but use the balance and reserved_balance of the parent.
            # However, pay_for_request updates reserved_balance and total_requests.
            # To stay simple, we charge the parent's balance and update parent's total_requests.
            return parent
        else:
            logger.error(
                "Parent key not found for child key",
                extra={
                    "child_key_hash": key.hashed_key[:8] + "...",
                    "parent_key_hash": key.parent_key_hash[:8] + "...",
                },
            )
    return key


async def pay_for_request(
    key: ApiKey, cost_per_request: int, session: AsyncSession
) -> int:
    """Process payment for a request."""
    # Ensure cost_per_request is at least the minimum allowed request cost
    cost_per_request = max(cost_per_request, settings.min_request_msat)

    billing_key = await get_billing_key(key, session)

    logger.info(
        "Processing payment for request",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "current_balance": billing_key.balance,
            "required_cost": cost_per_request,
            "sufficient_balance": billing_key.balance >= cost_per_request,
        },
    )

    if billing_key.total_balance < cost_per_request:
        logger.warning(
            "Insufficient balance for request",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "billing_key_hash": billing_key.hashed_key[:8] + "...",
                "balance": billing_key.balance,
                "reserved_balance": billing_key.reserved_balance,
                "required": cost_per_request,
                "shortfall": cost_per_request - billing_key.total_balance,
            },
        )

        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": f"Insufficient balance: {cost_per_request} mSats required. {billing_key.total_balance} available. (reserved: {billing_key.reserved_balance})",
                    "type": "insufficient_quota",
                    "code": "insufficient_balance",
                }
            },
        )

    # Check validity date
    if key.validity_date is not None:
        if time.time() > key.validity_date:
            logger.warning(
                "Key validity date expired",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "validity_date": key.validity_date,
                    "current_time": time.time(),
                },
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": {
                        "message": "API key has expired (validity date reached).",
                        "type": "invalid_request_error",
                        "code": "key_expired",
                    }
                },
            )

    # Check balance limit for child keys (or any key with a limit)
    if key.balance_limit is not None:
        await check_and_reset_limit(key, session)

        if (
            key.total_spent + key.reserved_balance + cost_per_request
            > key.balance_limit
        ):
            logger.warning(
                "Balance limit exceeded",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "total_spent": key.total_spent,
                    "reserved": key.reserved_balance,
                    "balance_limit": key.balance_limit,
                    "required": cost_per_request,
                },
            )
            raise HTTPException(
                status_code=402,
                detail={
                    "error": {
                        "message": f"Balance limit exceeded: {key.balance_limit} mSats limit. {key.total_spent} already spent ({key.reserved_balance} reserved), {cost_per_request} required for this request.",
                        "type": "insufficient_quota",
                        "code": "balance_limit_exceeded",
                    }
                },
            )

    logger.debug(
        "Charging base cost for request",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "cost": cost_per_request,
            "balance_before": billing_key.balance,
        },
    )

    # Charge the base cost for the request atomically to avoid race conditions
    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
        .where(col(ApiKey.balance) - col(ApiKey.reserved_balance) >= cost_per_request)
        .values(
            reserved_balance=col(ApiKey.reserved_balance) + cost_per_request,
            total_requests=col(ApiKey.total_requests) + 1,
        )
    )
    result = await session.exec(stmt)  # type: ignore[call-overload]

    # Also increment total_requests and reserved_balance on the child key if it's different
    if billing_key.hashed_key != key.hashed_key:
        child_stmt = (
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == key.hashed_key)
            .values(
                total_requests=col(ApiKey.total_requests) + 1,
                reserved_balance=col(ApiKey.reserved_balance) + cost_per_request,
            )
        )
        await session.exec(child_stmt)  # type: ignore[call-overload]

    await session.commit()

    if result.rowcount == 0:
        logger.error(
            "Concurrent request depleted balance",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "billing_key_hash": billing_key.hashed_key[:8] + "...",
                "required_cost": cost_per_request,
                "current_balance": billing_key.balance,
            },
        )

        # Another concurrent request spent the balance first
        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": f"Insufficient balance: {cost_per_request} mSats required. {billing_key.balance} available.",
                    "type": "insufficient_quota",
                    "code": "insufficient_balance",
                }
            },
        )

    await session.refresh(billing_key)
    if billing_key.hashed_key != key.hashed_key:
        await session.refresh(key)

    logger.info(
        "Payment processed successfully",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "charged_amount": cost_per_request,
            "new_balance": billing_key.balance,
            "total_spent": billing_key.total_spent,
            "total_requests": billing_key.total_requests,
        },
    )

    return cost_per_request


async def revert_pay_for_request(
    key: ApiKey, session: AsyncSession, cost_per_request: int
) -> None:
    billing_key = await get_billing_key(key, session)

    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
        .values(
            reserved_balance=col(ApiKey.reserved_balance) - cost_per_request,
            total_requests=col(ApiKey.total_requests) - 1,
        )
    )

    result = await session.exec(stmt)  # type: ignore[call-overload]

    # Also decrement total_requests and reserved_balance on the child key if it's different
    if billing_key.hashed_key != key.hashed_key:
        child_stmt = (
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == key.hashed_key)
            .values(
                total_requests=col(ApiKey.total_requests) - 1,
                reserved_balance=col(ApiKey.reserved_balance) - cost_per_request,
            )
        )
        await session.exec(child_stmt)  # type: ignore[call-overload]

    await session.commit()
    if result.rowcount == 0:
        logger.error(
            "Failed to revert payment - insufficient reserved balance",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "billing_key_hash": billing_key.hashed_key[:8] + "...",
                "cost_to_revert": cost_per_request,
                "current_reserved_balance": billing_key.reserved_balance,
            },
        )
        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": f"failed to revert request payment: {cost_per_request} mSats required. {billing_key.balance} available.",
                    "type": "payment_error",
                    "code": "payment_error",
                }
            },
        )
    await session.refresh(billing_key)
    if billing_key.hashed_key != key.hashed_key:
        await session.refresh(key)


async def adjust_payment_for_tokens(
    key: ApiKey, response_data: dict, session: AsyncSession, deducted_max_cost: int
) -> dict:
    """
    Adjusts the payment based on token usage in the response.
    This is called after the initial payment and the upstream request is complete.
    Returns cost data to be included in the response.
    """
    billing_key = await get_billing_key(key, session)
    model = response_data.get("model", "unknown")

    logger.debug(
        "Starting payment adjustment for tokens",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "billing_key_hash": billing_key.hashed_key[:8] + "...",
            "model": model,
            "deducted_max_cost": deducted_max_cost,
            "current_balance": billing_key.balance,
            "has_usage": "usage" in response_data,
        },
    )

    async def release_reservation_only() -> None:
        """Fallback to release reservation without charging when main update fails."""
        try:
            release_stmt = (
                update(ApiKey)
                .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
                .values(
                    reserved_balance=col(ApiKey.reserved_balance) - deducted_max_cost
                )
            )
            await session.exec(release_stmt)  # type: ignore[call-overload]

            # Also release on child key if it's different
            if billing_key.hashed_key != key.hashed_key:
                child_release_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == key.hashed_key)
                    .values(
                        reserved_balance=col(ApiKey.reserved_balance)
                        - deducted_max_cost
                    )
                )
                await session.exec(child_release_stmt)  # type: ignore[call-overload]

            await session.commit()
            logger.warning(
                "Released reservation without charging (fallback)",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "billing_key_hash": billing_key.hashed_key[:8] + "...",
                    "deducted_max_cost": deducted_max_cost,
                },
            )
        except Exception as e:
            logger.error(
                "Failed to release reservation in fallback",
                extra={
                    "error": str(e),
                    "key_hash": key.hashed_key[:8] + "...",
                    "billing_key_hash": billing_key.hashed_key[:8] + "...",
                },
            )

    match await calculate_cost(response_data, deducted_max_cost, session):
        case MaxCostData() as cost:
            logger.debug(
                "Using max cost data (no token adjustment)",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "billing_key_hash": billing_key.hashed_key[:8] + "...",
                    "model": model,
                    "max_cost": cost.total_msats,
                },
            )
            # Finalize by releasing reservation and charging max cost
            finalize_stmt = (
                update(ApiKey)
                .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
                .values(
                    reserved_balance=col(ApiKey.reserved_balance) - deducted_max_cost,
                    balance=col(ApiKey.balance) - cost.total_msats,
                    total_spent=col(ApiKey.total_spent) + cost.total_msats,
                )
            )
            result = await session.exec(finalize_stmt)  # type: ignore[call-overload]

            # Also update total_spent and reserved_balance on the child key if it's different
            if billing_key.hashed_key != key.hashed_key:
                child_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == key.hashed_key)
                    .values(
                        total_spent=col(ApiKey.total_spent) + cost.total_msats,
                        reserved_balance=col(ApiKey.reserved_balance)
                        - deducted_max_cost,
                    )
                )
                await session.exec(child_stmt)  # type: ignore[call-overload]

            await session.commit()
            if result.rowcount == 0:
                logger.error(
                    "Failed to finalize max-cost payment - retrying reservation release",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "deducted_max_cost": deducted_max_cost,
                        "current_reserved_balance": billing_key.reserved_balance,
                        "total_cost": cost.total_msats,
                        "model": model,
                    },
                )
                await release_reservation_only()
            else:
                await session.refresh(billing_key)
                if billing_key.hashed_key != key.hashed_key:
                    await session.refresh(key)
                logger.info(
                    "Max cost payment finalized",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "charged_amount": cost.total_msats,
                        "new_balance": billing_key.balance,
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
                    "billing_key_hash": billing_key.hashed_key[:8] + "...",
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
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "model": model,
                    },
                )
                finalize_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
                    .values(
                        reserved_balance=col(ApiKey.reserved_balance)
                        - deducted_max_cost,
                        balance=col(ApiKey.balance) - total_cost_msats,
                        total_spent=col(ApiKey.total_spent) + total_cost_msats,
                    )
                )
                await session.exec(finalize_stmt)  # type: ignore[call-overload]

                # Also update total_spent and reserved_balance on the child key if it's different
                if billing_key.hashed_key != key.hashed_key:
                    child_stmt = (
                        update(ApiKey)
                        .where(col(ApiKey.hashed_key) == key.hashed_key)
                        .values(
                            total_spent=col(ApiKey.total_spent) + total_cost_msats,
                            reserved_balance=col(ApiKey.reserved_balance)
                            - deducted_max_cost,
                        )
                    )
                    await session.exec(child_stmt)  # type: ignore[call-overload]

                await session.commit()
                await session.refresh(billing_key)
                if billing_key.hashed_key != key.hashed_key:
                    await session.refresh(key)
                return cost.dict()

            # this should never happen why do we handle this???
            if cost_difference > 0:
                # Need to charge more than reserved, finalize by releasing reservation and charging total
                logger.info(
                    "Additional charge required for token usage",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "additional_charge": cost_difference,
                        "current_balance": billing_key.balance,
                        "sufficient_balance": billing_key.balance >= cost_difference,
                        "model": model,
                    },
                )

                finalize_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
                    .values(
                        reserved_balance=col(ApiKey.reserved_balance)
                        - deducted_max_cost,
                        balance=col(ApiKey.balance) - total_cost_msats,
                        total_spent=col(ApiKey.total_spent) + total_cost_msats,
                    )
                )
                result = await session.exec(finalize_stmt)  # type: ignore[call-overload]

                # Also update total_spent and reserved_balance on the child key if it's different
                if billing_key.hashed_key != key.hashed_key:
                    child_stmt = (
                        update(ApiKey)
                        .where(col(ApiKey.hashed_key) == key.hashed_key)
                        .values(
                            total_spent=col(ApiKey.total_spent) + total_cost_msats,
                            reserved_balance=col(ApiKey.reserved_balance)
                            - deducted_max_cost,
                        )
                    )
                    await session.exec(child_stmt)  # type: ignore[call-overload]

                await session.commit()

                if result.rowcount:
                    cost.total_msats = total_cost_msats
                    await session.refresh(billing_key)
                    if billing_key.hashed_key != key.hashed_key:
                        await session.refresh(key)

                    logger.info(
                        "Finalized payment with additional charge",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "billing_key_hash": billing_key.hashed_key[:8] + "...",
                            "charged_amount": total_cost_msats,
                            "new_balance": billing_key.balance,
                            "model": model,
                        },
                    )
                else:
                    logger.warning(
                        "Failed to finalize additional charge - releasing reservation",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "billing_key_hash": billing_key.hashed_key[:8] + "...",
                            "attempted_charge": total_cost_msats,
                            "model": model,
                        },
                    )
                    await release_reservation_only()
            else:
                # Refund some of the base cost
                refund = abs(cost_difference)
                logger.info(
                    "Refunding excess payment",
                    extra={
                        "key_hash": key.hashed_key[:8] + "...",
                        "billing_key_hash": billing_key.hashed_key[:8] + "...",
                        "refund_amount": refund,
                        "current_balance": billing_key.balance,
                        "model": model,
                    },
                )

                refund_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == billing_key.hashed_key)
                    .values(
                        reserved_balance=col(ApiKey.reserved_balance)
                        - deducted_max_cost,
                        balance=col(ApiKey.balance) - total_cost_msats,
                        total_spent=col(ApiKey.total_spent) + total_cost_msats,
                    )
                )
                result = await session.exec(refund_stmt)  # type: ignore[call-overload]

                # Also update total_spent and reserved_balance on the child key if it's different
                if billing_key.hashed_key != key.hashed_key:
                    child_stmt = (
                        update(ApiKey)
                        .where(col(ApiKey.hashed_key) == key.hashed_key)
                        .values(
                            total_spent=col(ApiKey.total_spent) + total_cost_msats,
                            reserved_balance=col(ApiKey.reserved_balance)
                            - deducted_max_cost,
                        )
                    )
                    await session.exec(child_stmt)  # type: ignore[call-overload]

                await session.commit()

                if result.rowcount == 0:
                    logger.error(
                        "Failed to finalize payment - releasing reservation",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "billing_key_hash": billing_key.hashed_key[:8] + "...",
                            "deducted_max_cost": deducted_max_cost,
                            "current_reserved_balance": billing_key.reserved_balance,
                            "total_cost": total_cost_msats,
                            "model": model,
                        },
                    )
                    await release_reservation_only()
                else:
                    cost.total_msats = total_cost_msats
                    await session.refresh(billing_key)
                    if billing_key.hashed_key != key.hashed_key:
                        await session.refresh(key)

                    logger.info(
                        "Refund processed successfully",
                        extra={
                            "key_hash": key.hashed_key[:8] + "...",
                            "billing_key_hash": billing_key.hashed_key[:8] + "...",
                            "refunded_amount": refund,
                            "new_balance": billing_key.balance,
                            "final_cost": cost.total_msats,
                            "model": model,
                        },
                    )

            return cost.dict()

        case CostDataError() as error:
            logger.error(
                "Cost calculation error during payment adjustment - releasing reservation",
                extra={
                    "key_hash": key.hashed_key[:8] + "...",
                    "model": model,
                    "error_message": error.message,
                    "error_code": error.code,
                },
            )
            await release_reservation_only()

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
    # All calculate_cost variants are handled above.
    raise AssertionError("Unreachable: unhandled calculate_cost result")


async def periodic_key_reset() -> None:
    """Background task to reset key limits based on their policy."""
    from .core.db import create_session

    while True:
        try:
            interval = 3600  # Run every hour
            jitter = 300
            await asyncio.sleep(interval + random.uniform(0, jitter))
        except asyncio.CancelledError:
            break

        try:
            async with create_session() as session:
                # Find all keys that have a reset policy
                stmt = select(ApiKey).where(ApiKey.balance_limit_reset.is_not(None))  # type: ignore
                keys = (await session.exec(stmt)).all()

                now = int(time.time())
                updated_count = 0

                for key in keys:
                    reset_date = key.balance_limit_reset_date or 0
                    should_reset = False

                    if key.balance_limit_reset == "daily":
                        if (
                            datetime.fromtimestamp(now).date()
                            > datetime.fromtimestamp(reset_date).date()
                        ):
                            should_reset = True
                    elif key.balance_limit_reset == "weekly":
                        if (
                            datetime.fromtimestamp(now).isocalendar()[:2]
                            > datetime.fromtimestamp(reset_date).isocalendar()[:2]
                        ):
                            should_reset = True
                    elif key.balance_limit_reset == "monthly":
                        dt_now = datetime.fromtimestamp(now)
                        dt_reset = datetime.fromtimestamp(reset_date)
                        if dt_now.year > dt_reset.year or dt_now.month > dt_reset.month:
                            should_reset = True

                    if should_reset:
                        key.total_spent = 0
                        key.balance_limit_reset_date = now
                        session.add(key)
                        updated_count += 1

                if updated_count > 0:
                    await session.commit()
                    logger.info(
                        "Periodic key reset complete",
                        extra={"keys_reset": updated_count},
                    )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic_key_reset: {e}")
