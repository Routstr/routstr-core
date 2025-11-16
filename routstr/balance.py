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


# TODO: remove this endpoint when frontend is updated
@router.get("/", include_in_schema=False)
async def account_info(key: ApiKey = Depends(get_key_from_header)) -> dict:
    return {
        "api_key": "sk-" + key.hashed_key,
        "balance": key.balance,
        "reserved": key.reserved_balance,
    }


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
async def wallet_info(key: ApiKey = Depends(get_key_from_header)) -> dict:
    return {
        "api_key": "sk-" + key.hashed_key,
        "balance": key.balance,
        "reserved": key.reserved_balance,
    }


class TopupRequest(BaseModel):
    payment_data: str
    payment_method: str | None = None


@router.post("/topup")
async def topup_wallet_endpoint(
    payment_data: str | None = None,
    payment_method: str | None = None,
    cashu_token: str | None = None,
    topup_request: TopupRequest | None = None,
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    if topup_request is not None:
        payment_data = topup_request.payment_data
        payment_method = topup_request.payment_method or payment_method
    elif cashu_token is not None:
        payment_data = cashu_token
        payment_method = payment_method or "cashu"
    elif payment_data is None:
        raise HTTPException(
            status_code=400,
            detail="Payment data is required. Provide 'payment_data' or 'cashu_token' (deprecated).",
        )

    if payment_data is None:
        raise HTTPException(status_code=400, detail="Payment data is required.")

    payment_data = payment_data.replace("\n", "").replace("\r", "").replace("\t", "")

    try:
        amount_msats = await credit_balance(payment_data, key, session, payment_method)
    except ValueError as e:
        error_msg = str(e)
        if "already spent" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Payment already processed")
        elif "invalid" in error_msg.lower() or "decode" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Invalid payment data format")
        elif "could not detect" in error_msg.lower() or "unknown payment method" in error_msg.lower():
            raise HTTPException(status_code=400, detail=error_msg)
        else:
            raise HTTPException(status_code=400, detail="Failed to process payment")
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        logger.error(
            "Topup endpoint error",
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
    remaining_balance_msats: int = key.balance

    if key.refund_currency == "sat":
        remaining_balance = remaining_balance_msats // 1000
    else:
        remaining_balance = remaining_balance_msats

    if remaining_balance_msats > 0 and remaining_balance <= 0:
        raise HTTPException(status_code=400, detail="Balance too small to refund")
    elif remaining_balance <= 0:
        raise HTTPException(status_code=400, detail="No balance to refund")

    # Determine payment method from stored metadata
    # For backward compatibility, if refund_mint_url exists, assume cashu
    # TODO: Store payment_method in database for proper multi-method support
    from .payment.registry import get_payment_method, initialize_payment_methods

    initialize_payment_methods()
    payment_method_name = "cashu"
    if key.refund_mint_url:
        payment_method_name = "cashu"

    method = get_payment_method(payment_method_name) or get_payment_method("cashu")
    if not method:
        raise HTTPException(status_code=500, detail="Payment method not available")

    metadata = {
        "mint_url": key.refund_mint_url,
        "currency": key.refund_currency,
    }

    try:
        result = await method.refund_payment(
            remaining_balance_msats,
            key.refund_currency or "sat",
            key.refund_address,
            metadata,
            session,
        )
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        if (
            "mint" in error_msg.lower()
            or "connection" in error_msg.lower()
            or isinstance(e, Exception)
            and "ConnectError" in str(type(e))
        ):
            raise HTTPException(status_code=503, detail="Payment service unavailable")
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


balance_router.include_router(router)
deprecated_wallet_router = APIRouter(prefix="/v1/wallet", include_in_schema=False)
deprecated_wallet_router.include_router(router)
