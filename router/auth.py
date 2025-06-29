import hashlib
import json
import os
from typing import Optional

from fastapi import HTTPException
from sqlmodel import col, update

from .cashu import credit_balance
from .db import ApiKey, AsyncSession
from .models import MODELS

COST_PER_REQUEST = (
    int(os.environ.get("COST_PER_REQUEST", "1")) * 1000
)  # Convert to msats
COST_PER_1K_INPUT_TOKENS = (
    int(os.environ.get("COST_PER_1K_INPUT_TOKENS", "0")) * 1000
)  # Convert to msats
COST_PER_1K_OUTPUT_TOKENS = (
    int(os.environ.get("COST_PER_1K_OUTPUT_TOKENS", "0")) * 1000
)  # Convert to msats
MODEL_BASED_PRICING = os.environ.get("MODEL_BASED_PRICING", "false").lower() == "true"

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


def base64_token_json(cashu_token: str) -> dict:
    import base64

    # Version 3 - JSON format
    encoded = cashu_token[6:]  # Remove "cashuA"
    # Add correct padding – (-len) % 4 equals 0,1,2,3
    encoded += "=" * ((-len(encoded)) % 4)

    decoded = base64.urlsafe_b64decode(encoded).decode()
    token_data = json.loads(decoded)

    return token_data


def base64_token_cbor(cashu_token: str) -> dict:
    import base64

    import cbor2

    encoded = cashu_token[6:]  # Remove "cashuB"
    encoded += "=" * ((-len(encoded)) % 4)
    decoded_bytes = base64.urlsafe_b64decode(encoded)
    token_data = cbor2.loads(decoded_bytes)
    return token_data


def check_token_balance(headers: dict, body: dict) -> None:
    if x_cashu := headers.get("x-cashu", None):
        cashu_token = x_cashu
    elif auth := headers.get("authorization", None):
        cashu_token = auth.split(" ")[1]
    else:
        raise HTTPException(status_code=401, detail="Unauthorized")
    COST_PER_REQUEST = get_max_cost_for_model(model=body["model"])
    if cashu_token.startswith("cashuA"):
        _token = base64_token_json(cashu_token)
        amount = sum(p["amount"] for t in _token["token"] for p in t["proofs"])
        unit = _token["unit"]
        if unit == "sat":
            amount *= 1000
        if amount < COST_PER_REQUEST:
            raise HTTPException(status_code=413, detail="Insufficient balance")
    elif cashu_token.startswith("cashuB"):
        _token = base64_token_cbor(cashu_token)
        amount = sum(p["a"] for t in _token["t"] for p in t["p"])
        unit = _token["u"]
        if unit == "sat":
            amount *= 1000
        if amount < COST_PER_REQUEST:
            raise HTTPException(status_code=413, detail="Insufficient balance")
    else:
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_max_cost_for_model(model: str) -> int:
    if model not in [model.id for model in MODELS]:
        return COST_PER_REQUEST
    for m in MODELS:
        if m.id == model:
            return m.sats_pricing.max_cost * 1000  # type: ignore
    return COST_PER_REQUEST


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
    cost_data: dict = {
        "base_msats": max_cost,
        "input_msats": 0,
        "output_msats": 0,
        "total_msats": max_cost,
    }

    # Check if we have usage data
    if "usage" not in response_data or response_data["usage"] is None:
        print("No usage data in response, using base cost only")
        return cost_data

    # Default to configured pricing
    MSATS_PER_1K_INPUT_TOKENS = COST_PER_1K_INPUT_TOKENS
    MSATS_PER_1K_OUTPUT_TOKENS = COST_PER_1K_OUTPUT_TOKENS

    if MODEL_BASED_PRICING and MODELS:
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
    cost_difference = token_based_cost - max_cost

    if cost_difference == 0:
        await session.commit()
        return cost_data  # No adjustment needed

    if cost_difference > 0:
        # Need to charge more
        if key.balance < cost_difference:
            print(
                f"Warning: Insufficient balance for token-based pricing adjustment: {key.hashed_key[:10]}..."
            )
            cost_data["warning"] = "Insufficient balance for full token-based pricing"
            cost_data["balance_shortage_msats"] = cost_difference - key.balance
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
                cost_data["total_msats"] = max_cost + cost_difference
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
        cost_data["total_msats"] = max_cost - refund
        await session.refresh(key)

    return cost_data
