import hashlib
from typing import Optional

from fastapi import HTTPException
from sqlmodel import col, update

from .cashu import credit_balance
from .db import ApiKey, AsyncSession
from .models import MODELS
from .payment.cost_caculation import (
    COST_PER_REQUEST,
    MODEL_BASED_PRICING,
    CostData,
    CostDataError,
    MaxCostData,
    calculate_cost,
)
from .payment.helpers import get_max_cost_for_model

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
    if not bearer_key:
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
        if existing_key := await session.get(ApiKey, bearer_key[3:]):
            if key_expiry_time is not None:
                existing_key.key_expiry_time = key_expiry_time
            if refund_address is not None:
                existing_key.refund_address = refund_address
            return existing_key

    if bearer_key.startswith("cashu"):
        try:
            hashed_key = hashlib.sha256(bearer_key.encode()).hexdigest()
            if existing_key := await session.get(ApiKey, hashed_key):
                if key_expiry_time is not None:
                    existing_key.key_expiry_time = key_expiry_time
                if refund_address is not None:
                    existing_key.refund_address = refund_address
                return existing_key

            new_key = ApiKey(
                hashed_key=hashed_key,
                balance=0,
                refund_address=refund_address,
                key_expiry_time=key_expiry_time,
            )
            session.add(new_key)
            await session.flush()
            msats = await credit_balance(bearer_key, new_key, session)
            if msats <= 0:
                raise Exception("Token redemption failed")
            await session.refresh(new_key)
            await session.commit()
            return new_key
        except Exception as e:
            print(f"Redemption failed: {e}")
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
    key: ApiKey,
    session: AsyncSession,
    body: dict,
) -> None:
    # Use global COST_PER_REQUEST as default, override if model-based pricing is enabled
    cost_per_request = COST_PER_REQUEST
    if MODEL_BASED_PRICING and MODELS:
        cost_per_request = get_max_cost_for_model(model=body["model"])

    if key.balance < cost_per_request:
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

    # Charge the base cost for the request atomically to avoid race conditions
    stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key.hashed_key)
        .where(col(ApiKey.balance) >= cost_per_request)
        .values(
            balance=col(ApiKey.balance) - cost_per_request,
            total_spent=col(ApiKey.total_spent) + cost_per_request,
            total_requests=col(ApiKey.total_requests) + 1,
        )
    )
    result = await session.exec(stmt)  # type: ignore[call-overload]
    await session.commit()
    if result.rowcount == 0:
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


async def adjust_payment_for_tokens(
    key: ApiKey, response_data: dict, session: AsyncSession
) -> dict:
    """
    Adjusts the payment based on token usage in the response.
    This is called after the initial payment and the upstream request is complete.
    Returns cost data to be included in the response.
    """
    max_cost = get_max_cost_for_model(model=response_data["model"])

    match calculate_cost(response_data, max_cost):
        case MaxCostData() as cost:
            return cost.dict()
        case CostData() as cost:
            # If token-based pricing is enabled and base cost is 0, use token-based cost
            # Otherwise, token cost is additional to the base cost
            cost_difference = cost.total_msats - max_cost

            if cost_difference == 0:
                await session.commit()
                return cost.dict()

            if cost_difference > 0:
                # Need to charge more
                if key.balance < cost_difference:
                    print(
                        f"Warning: Insufficient balance for token-based pricing adjustment: {key.hashed_key[:10]}..."
                    )
                    await session.commit()
                else:
                    charge_stmt = (
                        update(ApiKey)
                        .where(col(ApiKey.hashed_key) == key.hashed_key)
                        .where(col(ApiKey.balance) >= cost_difference)
                        .values(
                            balance=col(ApiKey.balance) - cost_difference,
                            total_spent=col(ApiKey.total_spent) + cost_difference,
                        )
                    )
                    result = await session.exec(charge_stmt)  # type: ignore[call-overload]
                    await session.commit()
                    if result.rowcount:
                        cost.total_msats = max_cost + cost_difference
                        await session.refresh(key)
            else:
                # Refund some of the base cost
                refund = abs(cost_difference)
                refund_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == key.hashed_key)
                    .values(
                        balance=col(ApiKey.balance) + refund,
                        total_spent=col(ApiKey.total_spent) - refund,
                    )
                )
                await session.exec(refund_stmt)  # type: ignore[call-overload]
                await session.commit()
                cost.total_msats = max_cost - refund
                await session.refresh(key)

            return cost.dict()
        case CostDataError() as error:
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
