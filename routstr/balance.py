import asyncio
import hashlib
from time import monotonic
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .auth import validate_bearer_key
from .core.db import ApiKey, AsyncSession, get_session
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
    from .auth import get_billing_key

    billing_key = await get_billing_key(key, session)
    return {
        "api_key": "sk-" + key.hashed_key,
        "balance": billing_key.balance,
        "reserved": billing_key.reserved_balance,
        "is_child": key.parent_key_hash is not None,
        "parent_key": "sk-" + key.parent_key_hash if key.parent_key_hash else None,
        "total_requests": key.total_requests,
        "total_spent": key.total_spent,
    }


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
    initial_balance_token: str, session: AsyncSession = Depends(get_session)
) -> dict:
    key = await validate_bearer_key(initial_balance_token, session)
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
    from .auth import get_billing_key

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
        else:
            raise HTTPException(status_code=400, detail="Failed to redeem token")
    except Exception:
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


@router.post("/refund")
async def refund_wallet_endpoint(
    authorization: Annotated[str, Header(...)],
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization. Use 'Bearer <cashu-token>' or 'Bearer <api-key>'",
        )

    bearer_value: str = authorization[7:]

    if cached := await _refund_cache_get(bearer_value):
        return cached

    key: ApiKey = await validate_bearer_key(bearer_value, session)

    if key.parent_key_hash:
        raise HTTPException(
            status_code=400,
            detail="Cannot refund child key. Please refund the parent key instead.",
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

    await session.delete(key)
    await session.commit()

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


@router.post("/child-key")
async def create_child_key(
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Creates a child API key that uses the parent's balance."""
    # Check if this is already a child key
    if key.parent_key_hash:
        raise HTTPException(
            status_code=400,
            detail="Cannot create a child key for another child key.",
        )

    cost = settings.child_key_cost

    if key.total_balance < cost:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient balance to create child key. {cost} mSats required.",
        )

    # Deduct cost from parent
    key.balance -= cost
    key.total_spent += cost
    session.add(key)

    # Generate new key
    import secrets

    new_key_raw = secrets.token_hex(32)
    new_key_hash = new_key_raw  # We use the raw key as the hash for sk- keys

    child_key = ApiKey(
        hashed_key=new_key_hash,
        balance=0,
        parent_key_hash=key.hashed_key,
    )
    session.add(child_key)
    await session.commit()

    return {
        "api_key": "sk-" + new_key_hash,
        "cost_msats": cost,
        "parent_balance": key.balance,
    }


balance_router.include_router(lightning_router)
balance_router.include_router(router)

deprecated_wallet_router = APIRouter(prefix="/v1/wallet", include_in_schema=False)
deprecated_wallet_router.include_router(router)
