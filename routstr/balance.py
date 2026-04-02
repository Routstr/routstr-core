import asyncio
import hashlib
import time
from time import monotonic
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import select

from .auth import get_billing_key, validate_bearer_key
from .core.db import ApiKey, AsyncSession, CashuTransaction, get_session
from .core.logging import get_logger
from .core.settings import settings
from .lightning import lightning_router
from .wallet import credit_balance, recieve_token, send_to_lnurl, send_token

router = APIRouter()
balance_router = APIRouter(prefix="/v1/balance")

logger = get_logger(__name__)


async def get_key_from_header(
    authorization: Annotated[str, Header(...)],
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    if authorization.startswith("Bearer "):
        return await validate_bearer_key(authorization[7:], session)

    raise HTTPException(
        status_code=401,
        detail="Invalid authorization. Use 'Bearer <cashu-token>' or 'Bearer <api-key>'",
    )


async def get_balance_info(key: ApiKey, session: AsyncSession) -> dict:
    billing_key = await get_billing_key(key, session)
    info = {
        "api_key": "sk-" + key.hashed_key,
        "balance": billing_key.balance,
        "reserved": billing_key.reserved_balance,
        "is_child": key.parent_key_hash is not None,
        "parent_key": "sk-" + key.parent_key_hash if key.parent_key_hash else None,
        "total_requests": key.total_requests,
        "total_spent": key.total_spent,
        "balance_limit": key.balance_limit,
        "balance_limit_reset": key.balance_limit_reset,
        "validity_date": key.validity_date,
    }

    if not key.parent_key_hash:
        # Fetch child keys if this is a parent key
        statement = select(ApiKey).where(ApiKey.parent_key_hash == key.hashed_key)
        results = await session.exec(statement)
        child_keys = results.all()
        if child_keys:
            info["child_keys"] = [
                {
                    "api_key": "sk-" + ck.hashed_key,
                    "total_requests": ck.total_requests,
                    "total_spent": ck.total_spent,
                    "balance_limit": ck.balance_limit,
                    "balance_limit_reset": ck.balance_limit_reset,
                    "validity_date": ck.validity_date,
                }
                for ck in child_keys
            ]

    return info


# TODO: remove this endpoint when frontend is updated
@router.get("/", include_in_schema=False)
async def account_info(
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await get_balance_info(key, session)


# TODO: Implement POST /v1/wallet/create endpoint
# This endpoint should accept:
# - cashu_token (required): The eCash token to deposit
# - refund_lnurl (optional): LNURL for refunds (instead of refund_address in validate_bearer_key)
# - refund_expiry (optional): Expiry timestamp for the key (maps to key_expiry_time in validate_bearer_key)
# The endpoint should:
# 1. Create a new wallet/API key from the cashu_token
# 2. Store refund_lnurl and refund_expiry in the database
# 3. Return the API key (rstr_...) and balance
# Note: validate_bearer_key already supports refund_address and key_expiry_time params


@router.get("/create")
async def create_balance(
    initial_balance_token: str,
    balance_limit: int | None = None,
    balance_limit_reset: str | None = None,
    validity_date: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    key = await validate_bearer_key(initial_balance_token, session)

    if balance_limit is not None or balance_limit_reset or validity_date:
        key.balance_limit = balance_limit
        key.balance_limit_reset = balance_limit_reset
        key.validity_date = validity_date
        if balance_limit_reset:
            key.balance_limit_reset_date = int(time.time())
        session.add(key)
        await session.commit()
        await session.refresh(key)

    return {
        "api_key": "sk-" + key.hashed_key,
        "balance": key.balance,
    }


@router.get("/info")
async def wallet_info(
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await get_balance_info(key, session)


class TopupRequest(BaseModel):
    cashu_token: str


@router.post("/topup")
async def topup_wallet_endpoint(
    cashu_token: str | None = None,
    topup_request: TopupRequest | None = None,
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    billing_key = await get_billing_key(key, session)

    if topup_request is not None:
        cashu_token = topup_request.cashu_token
    if cashu_token is None:
        raise HTTPException(status_code=400, detail="A cashu_token is required.")

    cashu_token = cashu_token.replace("\n", "").replace("\r", "").replace("\t", "")
    if len(cashu_token) < 10 or "cashu" not in cashu_token:
        raise HTTPException(status_code=400, detail="Invalid token format")
    try:
        amount_msats = await credit_balance(cashu_token, billing_key, session)
    except ValueError as e:
        error_msg = str(e)
        if "already spent" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Token already spent")
        elif "invalid" in error_msg.lower() or "decode" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Invalid token format")
        elif "insufficient" in error_msg.lower() or "melt fee" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail=f"Token value is too small to cover swap fees. {error_msg}",
            )
        elif "failed to melt" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail=f"Failed to swap foreign mint token. {error_msg}",
            )
        else:
            raise HTTPException(status_code=400, detail=f"Failed to redeem token: {error_msg}")
    except Exception as e:
        logger.error(
            "topup_wallet_endpoint: unhandled error",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise HTTPException(status_code=500, detail="Internal server error")
    return {"msats": amount_msats}


_REFUND_CACHE_TTL_SECONDS: int = settings.refund_cache_ttl_seconds
_refund_cache_lock: asyncio.Lock = asyncio.Lock()
_refund_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _cache_key_for_authorization(authorization: str) -> str:
    return hashlib.sha256(authorization.strip().encode()).hexdigest()


async def _refund_cache_get(authorization: str) -> dict[str, str] | None:
    key = _cache_key_for_authorization(authorization)
    async with _refund_cache_lock:
        item = _refund_cache.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= monotonic():
            del _refund_cache[key]
            return None
        return value


async def _refund_cache_set(authorization: str, value: dict[str, str]) -> None:
    key = _cache_key_for_authorization(authorization)
    expiry = monotonic() + _REFUND_CACHE_TTL_SECONDS
    async with _refund_cache_lock:
        _refund_cache[key] = (expiry, value)


@router.post("/refund", response_model=None)
async def refund_wallet_endpoint(
    authorization: Annotated[str | None, Header()] = None,
    x_cashu: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse | dict[str, str]:
    if x_cashu:
        # Find the "in" transaction by the original payment token
        in_tx_result = await session.exec(
            select(CashuTransaction).where(
                CashuTransaction.token == x_cashu,
                CashuTransaction.type == "in",
            )
        )
        in_tx = in_tx_result.first()
        if in_tx is None:
            raise HTTPException(status_code=404, detail="Refund not found")

        # Use the request_id to find the associated "out" (refund) transaction
        if in_tx.request_id is None:
            raise HTTPException(status_code=404, detail="Refund not found")

        out_tx_result = await session.exec(
            select(CashuTransaction).where(
                CashuTransaction.request_id == in_tx.request_id,
                CashuTransaction.type == "out",
            )
        )
        out_tx = out_tx_result.first()
        if out_tx is None:
            raise HTTPException(status_code=404, detail="Refund not found")
        if out_tx.swept:
            raise HTTPException(status_code=410, detail="Refund has been swept")

        out_tx.collected = True
        session.add(out_tx)
        await session.commit()
        body: dict[str, str] = {"token": out_tx.token}
        if out_tx.unit == "sat":
            body["sats"] = str(out_tx.amount)
        else:
            body["msats"] = str(out_tx.amount)
        return JSONResponse(content=body, headers={"X-Cashu": out_tx.token})

    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization. Use 'Bearer <cashu-token>' or 'Bearer <api-key>'",
        )

    bearer_value: str = authorization[7:]
    key: ApiKey = await validate_bearer_key(bearer_value, session)

    if key.total_balance <= 0:
        if cached := await _refund_cache_get(bearer_value):
            return cached

    if key.parent_key_hash:
        raise HTTPException(
            status_code=400,
            detail="Cannot refund child key. Please refund the parent key instead.",
        )

    if key.reserved_balance > 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot refund key. There are ongoing requests for this api key.",
        )

    remaining_balance_msats: int = key.total_balance

    if key.refund_currency == "sat":
        remaining_balance = remaining_balance_msats // 1000
    else:
        remaining_balance = remaining_balance_msats

    if remaining_balance_msats > 0 and remaining_balance <= 0:
        raise HTTPException(status_code=400, detail="Balance too small to refund")
    elif remaining_balance <= 0:
        raise HTTPException(status_code=400, detail="No balance to refund")

    # Perform refund operation first, before modifying balance
    try:
        if key.refund_address:
            from .core.settings import settings as global_settings

            await send_to_lnurl(
                remaining_balance,
                key.refund_currency or "sat",
                key.refund_mint_url or global_settings.primary_mint,
                key.refund_address,
            )
            result = {"recipient": key.refund_address}
        else:
            refund_currency = key.refund_currency or "sat"
            token = await send_token(
                remaining_balance, refund_currency, key.refund_mint_url
            )
            result = {"token": token}

        if key.refund_currency == "sat":
            result["sats"] = str(remaining_balance_msats // 1000)
        else:
            result["msats"] = str(remaining_balance_msats)

    except HTTPException:
        # Re-raise HTTP exceptions (like 400 for balance too small)
        raise
    except Exception as e:
        # If refund fails, don't modify the database
        error_msg = str(e)
        if (
            "mint" in error_msg.lower()
            or "connection" in error_msg.lower()
            or isinstance(e, Exception)
            and "ConnectError" in str(type(e))
        ):
            raise HTTPException(status_code=503, detail="Mint service unavailable")
        else:
            raise HTTPException(status_code=500, detail="Refund failed")

    await _refund_cache_set(bearer_value, result)

    previous_reserved_balance = key.reserved_balance
    key.balance = 0
    key.reserved_balance = 0
    session.add(key)
    await session.commit()

    logger.info(
        "refund_wallet_endpoint: refund successful",
        extra={
            "refunded_msats": remaining_balance_msats,
            "previous_reserved_balance": previous_reserved_balance,
        },
    )

    return result


@router.post("/donate")
async def donate(token: str, ref: str | None = None) -> str:
    try:
        amount, unit, _ = await recieve_token(token)
        if ref:
            logger.info(
                "donation received", extra={"ref": ref, "amount": amount, "unit": unit}
            )
        return "Thanks!"
    except Exception:
        return "Invalid token."


class ChildKeyRequest(BaseModel):
    count: int
    balance_limit: int | None = None
    balance_limit_reset: str | None = None
    validity_date: int | None = None


@router.post("/child-key")
async def create_child_key(
    payload: ChildKeyRequest,
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Creates one or more child API keys that use the parent's balance."""
    # Log incoming request for debugging
    logger.debug(f"Child key creation request: count={payload.count}")

    count = payload.count
    if count < 1 or count > 50:
        raise HTTPException(status_code=400, detail="Count must be between 1 and 50.")

    # Check if this is already a child key
    if key.parent_key_hash:
        raise HTTPException(
            status_code=400,
            detail="Cannot create a child key for another child key.",
        )

    cost_per_key = settings.child_key_cost
    total_cost = cost_per_key * count

    if key.total_balance < total_cost:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient balance to create {count} child keys. {total_cost} mSats required.",
        )

    # Deduct cost from parent
    key.balance -= total_cost
    key.total_spent += total_cost
    session.add(key)

    # Generate new keys
    import secrets

    new_keys = []
    for _ in range(count):
        new_key_raw = secrets.token_hex(32)
        new_key_hash = new_key_raw  # We use the raw key as the hash for sk- keys

        child_key = ApiKey(
            hashed_key=new_key_hash,
            balance=0,
            parent_key_hash=key.hashed_key,
            balance_limit=payload.balance_limit,
            balance_limit_reset=payload.balance_limit_reset,
            balance_limit_reset_date=int(time.time())
            if payload.balance_limit_reset
            else None,
            validity_date=payload.validity_date,
        )
        session.add(child_key)
        new_keys.append("sk-" + new_key_hash)

    await session.commit()

    response_data = {
        "api_keys": new_keys,
        "count": count,
        "cost_msats": total_cost,
        "cost_sats": total_cost // 1000,
        "parent_balance": key.balance,
        "parent_balance_sats": key.balance // 1000,
    }
    logger.debug(f"Child key creation response: {response_data}")
    return response_data


class ChildKeyResetRequest(BaseModel):
    child_key: str


@router.post("/child-key/reset")
async def reset_child_key_spent(
    payload: ChildKeyResetRequest,
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Resets the total_spent of a child key. Must be called by the parent."""
    child_key_raw = payload.child_key
    if child_key_raw.startswith("sk-"):
        child_key_raw = child_key_raw[3:]

    child_key = await session.get(ApiKey, child_key_raw)
    if not child_key:
        raise HTTPException(status_code=404, detail="Child key not found.")

    if child_key.parent_key_hash != key.hashed_key:
        raise HTTPException(
            status_code=403, detail="Unauthorized. You are not the parent of this key."
        )

    child_key.total_spent = 0
    if child_key.balance_limit_reset:
        child_key.balance_limit_reset_date = int(time.time())
    session.add(child_key)
    await session.commit()

    return {"success": True, "message": "Child key balance reset successfully."}


@router.get("/cashu-refund/{payment_token_hash}")
async def get_cashu_refund(
    payment_token_hash: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Retrieve a stored Cashu refund token by the hash of the original payment token."""
    result = await session.get(CashuTransaction, payment_token_hash)
    if result is None:
        raise HTTPException(status_code=404, detail="Refund not found")
    if result.swept:
        raise HTTPException(status_code=410, detail="Refund has been swept")
    result.collected = True
    session.add(result)
    await session.commit()
    return {
        "refund_token": result.token,
        "amount": result.amount,
        "unit": result.unit,
    }


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    include_in_schema=False,
    response_model=None,
)
async def wallet_catch_all(path: str) -> NoReturn:
    raise HTTPException(
        status_code=404, detail="Not found check /docs for available endpoints"
    )


balance_router.include_router(lightning_router)
balance_router.include_router(router)

deprecated_wallet_router = APIRouter(prefix="/v1/wallet", include_in_schema=False)
deprecated_wallet_router.include_router(router)
