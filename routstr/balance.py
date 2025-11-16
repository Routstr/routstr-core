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
from .payment.methods import get_payment_method
from .wallet import recieve_token

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
    cashu_token: str


@router.post("/topup")
async def topup_wallet_endpoint(
    cashu_token: str | None = None,
    topup_request: TopupRequest | None = None,
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    if topup_request is not None:
        cashu_token = topup_request.cashu_token
    if cashu_token is None:
        raise HTTPException(status_code=400, detail="A payment credential is required.")

    payment_credential = cashu_token.replace("\n", "").replace("\r", "").replace("\t", "")
    if len(payment_credential) < 10:
        raise HTTPException(status_code=400, detail="Invalid payment credential format")
    
    try:
        payment_method = get_payment_method(payment_credential)
        logger.info(
            "Processing topup with payment method",
            extra={
                "payment_method": payment_method.__class__.__name__,
                "key_hash": key.hashed_key[:8] + "...",
            },
        )
        
        payment_result = await payment_method.receive_payment(payment_credential, key, session)
        amount_msats = payment_result.amount_msats
        
        logger.info(
            "Topup successful",
            extra={
                "msats": amount_msats,
                "payment_method": payment_result.payment_method,
                "key_hash": key.hashed_key[:8] + "...",
            },
        )
    except ValueError as e:
        error_msg = str(e)
        if "no payment method" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Unsupported payment method")
        elif "already spent" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Payment already spent")
        elif "invalid" in error_msg.lower() or "decode" in error_msg.lower():
            raise HTTPException(status_code=400, detail="Invalid payment credential format")
        else:
            raise HTTPException(status_code=400, detail=f"Failed to process payment: {error_msg}")
    except NotImplementedError as nie:
        logger.warning(
            "Payment method not yet implemented",
            extra={"error": str(nie)},
        )
        raise HTTPException(
            status_code=501,
            detail=f"Payment method not yet implemented: {str(nie)}",
        )
    except Exception as e:
        logger.error(
            "Topup failed",
            extra={
                "error": str(e),
                "error_type": type(e).__name__,
            },
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

    # Perform refund operation first, before modifying balance
    try:
        # Determine payment method from key metadata
        # Default to Cashu for backward compatibility
        from .payment.methods import CashuPaymentMethod
        
        # Try to detect payment method from refund metadata
        # For now, we default to Cashu since it's the only fully implemented method
        # In the future, we should store payment_method_type in ApiKey model
        payment_method = CashuPaymentMethod()
        
        logger.info(
            "Processing refund",
            extra={
                "payment_method": payment_method.__class__.__name__,
                "key_hash": key.hashed_key[:8] + "...",
                "amount_msats": remaining_balance_msats,
            },
        )
        
        result = await payment_method.refund_payment(key, remaining_balance_msats)
        
        logger.info(
            "Refund successful",
            extra={
                "payment_method": payment_method.__class__.__name__,
                "key_hash": key.hashed_key[:8] + "...",
                "refund_details": str(result)[:100] + "...",
            },
        )

    except HTTPException:
        # Re-raise HTTP exceptions (like 400 for balance too small)
        raise
    except NotImplementedError as nie:
        logger.warning(
            "Refund method not yet implemented",
            extra={"error": str(nie)},
        )
        raise HTTPException(
            status_code=501,
            detail=f"Refund not yet implemented for this payment method: {str(nie)}",
        )
    except Exception as e:
        # If refund fails, don't modify the database
        error_msg = str(e)
        logger.error(
            "Refund failed",
            extra={
                "error": error_msg,
                "error_type": type(e).__name__,
            },
        )
        if (
            "mint" in error_msg.lower()
            or "connection" in error_msg.lower()
            or isinstance(e, Exception)
            and "ConnectError" in str(type(e))
        ):
            raise HTTPException(status_code=503, detail="Service unavailable")
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
