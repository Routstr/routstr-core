import asyncio
import hashlib
import json
from typing import Optional


from fastapi import HTTPException, Request

from .cashu import credit_balance, pay_out
from .db import ApiKey, AsyncSession
from .models import MODELS
from .settings import (
    COST_PER_REQUEST,
    COST_PER_1K_INPUT_TOKENS,
    COST_PER_1K_OUTPUT_TOKENS,
    MODEL_BASED_PRICING,
)


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
            await credit_balance(
                bearer_key, new_key, session
            )  # TODO: see cashu.py "_initialize_wallet"
            await session.refresh(new_key)
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
    request: Request | None,
    request_body: bytes | None = None,
) -> None:
    if MODEL_BASED_PRICING and os.path.exists("models.json"):
        if request_body:
            body = json.loads(request_body)
        else:
            body = await request.json()  # type: ignore
        if request_model := body.get("model"):
            if request_model not in [model.id for model in MODELS]:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": {
                            "message": f"Invalid model: {request_model}",
                            "type": "invalid_request_error",
                            "code": "model_not_found",
                        }
                    },
                )
            model = next(model for model in MODELS if model.id == request_model)
            if key.balance < model.sats_pricing.max_cost * 1000:  # type: ignore
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error": {
                            "message": f"This model requires a minimum balance of {model.sats_pricing.max_cost} sats",  # type: ignore
                            "type": "insufficient_quota",
                            "code": "insufficient_balance",
                        }
                    },
                )

    if key.balance < COST_PER_REQUEST:
        raise HTTPException(
            status_code=402,
            detail={
                "error": {
                    "message": f"Insufficient balance: {COST_PER_REQUEST} mSats required. {key.balance} available.",
                    "type": "insufficient_quota",
                    "code": "insufficient_balance",
                }
            },
        )

    # Charge the base cost for the request
    key.balance -= COST_PER_REQUEST
    key.total_spent += COST_PER_REQUEST
    key.total_requests += 1
    session.add(key)
    await session.commit()
    await session.refresh(key)


async def adjust_payment_for_tokens(
    key: ApiKey, response_data: dict, session: AsyncSession
) -> dict:
    """
    Adjusts the payment based on token usage in the response.
    This is called after the initial payment and the upstream request is complete.
    Returns cost data to be included in the response.
    """
    cost_data: dict = {
        "base_msats": COST_PER_REQUEST,
        "input_msats": 0,
        "output_msats": 0,
        "total_msats": COST_PER_REQUEST,
    }

    # Check if we have usage data
    if "usage" not in response_data or response_data["usage"] is None:
        print("No usage data in response, using base cost only")
        return cost_data

    # Default to configured pricing
    MSATS_PER_1K_INPUT_TOKENS = COST_PER_1K_INPUT_TOKENS
    MSATS_PER_1K_OUTPUT_TOKENS = COST_PER_1K_OUTPUT_TOKENS

    if MODEL_BASED_PRICING and os.path.exists("models.json"):
        response_model = response_data.get("model", "")
        if response_model not in [model.id for model in MODELS]:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": f"Invalid model in response: {response_model}",
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
            )
        model = next(model for model in MODELS if model.id == response_model)
        if model.sats_pricing is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "Model pricing not defined",
                        "type": "invalid_request_error",
                        "code": "pricing_not_found",
                    }
                },
            )

        MSATS_PER_1K_INPUT_TOKENS = model.sats_pricing.prompt * 1_000_000  # type: ignore
        MSATS_PER_1K_OUTPUT_TOKENS = model.sats_pricing.completion * 1_000_000  # type: ignore

    if not (MSATS_PER_1K_OUTPUT_TOKENS and MSATS_PER_1K_INPUT_TOKENS):
        # If no token pricing is configured, just return base cost
        return cost_data

    input_tokens = response_data.get("usage", {}).get("prompt_tokens", 0)
    output_tokens = response_data.get("usage", {}).get("completion_tokens", 0)

    input_msats = int(round(input_tokens / 1000 * MSATS_PER_1K_INPUT_TOKENS, 0))
    output_msats = int(round(output_tokens / 1000 * MSATS_PER_1K_OUTPUT_TOKENS, 0))
    token_based_cost = int(round(input_msats + output_msats, 0))

    cost_data["base_msats"] = 0
    cost_data["input_msats"] = input_msats
    cost_data["output_msats"] = output_msats
    cost_data["total_msats"] = token_based_cost

    # If token-based pricing is enabled and base cost is 0, use token-based cost
    # Otherwise, token cost is additional to the base cost
    cost_difference = token_based_cost - COST_PER_REQUEST

    if cost_difference == 0:
        return cost_data  # No adjustment needed

    if cost_difference > 0:
        # Need to charge more
        if key.balance < cost_difference:
            print(
                f"Warning: Insufficient balance for token-based pricing adjustment: {key.hashed_key[:10]}..."
            )
            # Still proceed but log the issue - we already provided the service
            # Add information about insufficient balance to cost data
            cost_data["warning"] = "Insufficient balance for full token-based pricing"
            cost_data["balance_shortage_msats"] = cost_difference - key.balance
        else:
            key.balance -= cost_difference
            key.total_spent += cost_difference
            cost_data["total_msats"] = COST_PER_REQUEST + cost_difference
    else:
        # Refund some of the base cost
        refund = abs(cost_difference)
        key.balance += refund
        key.total_spent -= refund
        cost_data["total_msats"] = COST_PER_REQUEST - refund

    session.add(key)
    await session.commit()

    asyncio.create_task(pay_out())

    return cost_data
