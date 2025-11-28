import json
import math

from fastapi import HTTPException, Response
from fastapi.requests import Request
from sqlmodel import col, update

from ..core import get_logger
from ..core.db import AsyncSession, TemporaryCredit
from ..payment.cost import CostData, CostDataError, MaxCostData, calculate_cost

logger = get_logger(__name__)


# TODO: remove and replace with custom HTTPException
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


# Request payment handlers, todo: maybe mode somewhere else...


async def pay_for_request(
    key: TemporaryCredit, cost_per_request: int, session: AsyncSession
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

    if key.total_balance_msat < cost_per_request:
        logger.warning(
            "Insufficient balance for request",
            extra={
                "key_hash": key.hashed_key[:8] + "...",
                "balance": key.balance,
                "reserved_balance": key.reserved_balance,
                "required": cost_per_request,
                "shortfall": cost_per_request - key.total_balance_msat,
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
        update(TemporaryCredit)
        .where(col(TemporaryCredit.hashed_key) == key.hashed_key)
        .where(col(TemporaryCredit.balance) >= cost_per_request)
        .values(
            reserved_balance=col(TemporaryCredit.reserved_balance) + cost_per_request
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

    logger.info(
        "Payment processed successfully",
        extra={
            "key_hash": key.hashed_key[:8] + "...",
            "charged_amount": cost_per_request,
            "new_balance": key.balance,
        },
    )

    return cost_per_request


async def revert_pay_for_request(
    key: TemporaryCredit, session: AsyncSession, cost_per_request: int
) -> None:
    stmt = (
        update(TemporaryCredit)
        .where(col(TemporaryCredit.hashed_key) == key.hashed_key)
        .values(
            reserved_balance=col(TemporaryCredit.reserved_balance) - cost_per_request
        )
    )

    result = await session.exec(stmt)  # type: ignore[call-overload]
    await session.commit()
    if result.rowcount == 0:
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
    key: TemporaryCredit,
    response_data: dict,
    session: AsyncSession,
    deducted_max_cost: int,
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
                update(TemporaryCredit)
                .where(col(TemporaryCredit.hashed_key) == key.hashed_key)
                .values(
                    reserved_balance=col(TemporaryCredit.reserved_balance)
                    - deducted_max_cost,
                    balance=col(TemporaryCredit.balance) - cost.total_msats,
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
                    update(TemporaryCredit)
                    .where(col(TemporaryCredit.hashed_key) == key.hashed_key)
                    .values(
                        reserved_balance=col(TemporaryCredit.reserved_balance)
                        - deducted_max_cost,
                        balance=col(TemporaryCredit.balance) - total_cost_msats,
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
                    update(TemporaryCredit)
                    .where(col(TemporaryCredit.hashed_key) == key.hashed_key)
                    .values(
                        reserved_balance=col(TemporaryCredit.reserved_balance)
                        - deducted_max_cost,
                        balance=col(TemporaryCredit.balance) - total_cost_msats,
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
                    update(TemporaryCredit)
                    .where(col(TemporaryCredit.hashed_key) == key.hashed_key)
                    .values(
                        reserved_balance=col(TemporaryCredit.reserved_balance)
                        - deducted_max_cost,
                        balance=col(TemporaryCredit.balance) - total_cost_msats,
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
