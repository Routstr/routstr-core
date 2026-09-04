import asyncio
import hashlib
from time import monotonic
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import col, select, update

from .auth import (
    redemption_error_to_http_exception,
    validate_bearer_key,
)
from .core.db import (
    ApiKey,
    AsyncSession,
    CashuTransaction,
    get_session,
    release_stale_reservations,
)
from .core.db import (
    store_cashu_transaction_with_retry as store_cashu_transaction,
)
from .core.logging import get_logger
from .core.settings import settings
from .lightning import lightning_router
from .payment.lnurl import MeltOutcomeAmbiguousError
from .wallet import (
    classify_redemption_error,
    credit_balance,
    is_mint_connection_error,
    recieve_token,
    send_to_lnurl,
    send_token,
    token_mint_url,
)

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
    info = {
        "api_key": "sk-" + key.hashed_key,
        "balance": key.total_balance,
        "reserved": key.reserved_balance,
        "total_requests": key.total_requests,
        "total_spent": key.total_spent,
        "validity_date": key.validity_date,
    }

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


class BalanceCreateRequest(BaseModel):
    initial_balance_token: str
    validity_date: int | None = None


async def _create_balance(
    initial_balance_token: str,
    validity_date: int | None,
    session: AsyncSession,
) -> dict:
    key = await validate_bearer_key(initial_balance_token, session)

    if validity_date is not None:
        key.validity_date = validity_date
        session.add(key)
        await session.commit()
        await session.refresh(key)

    return {
        "api_key": "sk-" + key.hashed_key,
        "balance": key.balance,
    }


@router.post("/create")
async def create_balance_from_body(
    payload: BalanceCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _create_balance(
        payload.initial_balance_token,
        payload.validity_date,
        session,
    )


@router.get("/create")
async def create_balance(
    initial_balance_token: str,
    validity_date: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _create_balance(
        initial_balance_token,
        validity_date,
        session,
    )


@router.get("/info")
async def wallet_info(
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await get_balance_info(key, session)


class TopupRequest(BaseModel):
    cashu_token: str


def _error_chain(error: BaseException) -> list[dict[str, str]]:
    chain: list[dict[str, str]] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({"type": type(current).__name__, "message": str(current)})
        current = current.__cause__ or current.__context__
    return chain


@router.post("/topup")
async def topup_wallet_endpoint(
    cashu_token: str | None = None,
    topup_request: TopupRequest | None = None,
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    billing_key = key

    if topup_request is not None:
        cashu_token = topup_request.cashu_token
    if cashu_token is None:
        raise HTTPException(status_code=400, detail="A cashu_token is required.")

    cashu_token = cashu_token.replace("\n", "").replace("\r", "").replace("\t", "")
    if len(cashu_token) < 10 or "cashu" not in cashu_token:
        raise HTTPException(status_code=400, detail="Invalid token format")

    source_mint = token_mint_url(cashu_token, "unknown")
    logger.info(
        "Cashu wallet top-up started",
        extra={
            "event": "cashu_topup_started",
            "source_mint": source_mint,
            "primary_mint": settings.primary_mint,
            "trusted_mints": settings.cashu_mints,
            "key_hash": billing_key.hashed_key[:8],
        },
    )
    try:
        amount_msats = await credit_balance(cashu_token, billing_key, session)
    except Exception as e:
        # Shared taxonomy so top-up matches the bearer/X-Cashu paths (503 for an
        # unreachable mint, 422 for fee/swap failures, 400 for token faults).
        classified = classify_redemption_error(e)
        if classified is None:
            logger.error(
                "Cashu wallet top-up failed with an unhandled error",
                extra={
                    "event": "cashu_topup_failed",
                    "source_mint": source_mint,
                    "primary_mint": settings.primary_mint,
                    "trusted_mints": settings.cashu_mints,
                    "error_chain": _error_chain(e),
                },
            )
            raise redemption_error_to_http_exception(e)
        error_type, status_code, message, error_code = classified
        logger.warning(
            "Cashu wallet top-up failed",
            extra={
                "event": "cashu_topup_failed",
                "source_mint": source_mint,
                "primary_mint": settings.primary_mint,
                "trusted_mints": settings.cashu_mints,
                "status_code": status_code,
                "error_type": error_type,
                "error_code": error_code,
                "error_chain": _error_chain(e),
            },
        )
        raise redemption_error_to_http_exception(e)

    logger.info(
        "Cashu wallet top-up completed",
        extra={
            "event": "cashu_topup_completed",
            "source_mint": source_mint,
            "credited_msats": amount_msats,
            "key_hash": billing_key.hashed_key[:8],
        },
    )
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


async def _lookup_key_no_create(
    bearer_value: str, session: AsyncSession
) -> ApiKey | None:
    """Look up an existing API key without creating one Used by the refund endpoint"""
    if bearer_value.startswith("sk-"):
        return await session.get(ApiKey, bearer_value[3:])
    if bearer_value.startswith("cashu"):
        hashed = hashlib.sha256(bearer_value.encode()).hexdigest()
        return await session.get(ApiKey, hashed)
    return None


async def _get_persisted_api_key_refund(
    key: ApiKey, session: AsyncSession
) -> dict[str, str] | None:
    result = await session.exec(
        select(CashuTransaction)
        .where(
            CashuTransaction.api_key_hashed_key == key.hashed_key,
            CashuTransaction.type == "out",
            CashuTransaction.source == "apikey",
        )
        .order_by(col(CashuTransaction.created_at).desc())
    )
    refund = result.first()
    if refund is None:
        return None
    if refund.swept:
        raise HTTPException(status_code=410, detail="Refund has been swept")

    refund.collected = True
    session.add(refund)
    await session.commit()

    persisted = {"token": refund.token}
    if refund.unit == "sat":
        persisted["sats"] = str(refund.amount)
    else:
        persisted["msats"] = str(refund.amount)
    return persisted


async def _restore_balance(
    session: AsyncSession,
    hashed_key: str,
    balance: int,
    reserved_balance: int,
    mint_url: str,
) -> None:
    """Restore balance after a failed refund mint attempt."""
    restore_stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == hashed_key)
        .values(
            balance=col(ApiKey.balance) + balance,
            reserved_balance=col(ApiKey.reserved_balance) + reserved_balance,
        )
    )
    await session.exec(restore_stmt)  # type: ignore[call-overload]
    await session.commit()
    logger.info(
        "refund_wallet_endpoint: balance restored after mint failure",
        extra={
            "key_hash": hashed_key[:8],
            "restored_balance": balance,
            "mint_url": mint_url,
        },
    )


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
            # The "in" row exists with a request_id, but the "out" (refund)
            # row hasn't been written yet — the upstream request is still in
            # flight and the refund will be minted once it completes. Tell the
            # client to retry instead of 404ing permanently (race condition
            # where /v1/wallet/refund is polled before the refund exists).
            logger.debug(
                "refund_wallet_endpoint: refund pending (in row exists, out row not yet created)",
                extra={"request_id": in_tx.request_id},
            )
            raise HTTPException(
                status_code=425,
                detail="Refund is pending; retry shortly.",
                headers={"Retry-After": "2"},
            )
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
    key: ApiKey | None = await _lookup_key_no_create(bearer_value, session)
    if key is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Key not found. Deposit first via /v1/wallet/create before requesting a refund.",
                    "type": "invalid_request_error",
                    "code": "key_not_found",
                }
            },
        )

    if key.total_balance <= 0:
        if cached := await _refund_cache_get(bearer_value):
            return cached
        if persisted := await _get_persisted_api_key_refund(key, session):
            return persisted

    if key.reserved_balance > 0:
        # Release only durable reservations old enough to be stale. A newer
        # request on the same aggregate balance must remain reserved.
        await release_stale_reservations(
            session,
            settings.stale_reservation_timeout_seconds,
            key_hash=key.hashed_key,
        )
        await session.refresh(key)
        if key.reserved_balance > 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot refund key. There are ongoing requests for this api key.",
            )
        logger.warning(
            "refund_wallet_endpoint: released stale reservation before refund",
            extra={
                "key_hash": key.hashed_key[:8],
                "stale_timeout_seconds": settings.stale_reservation_timeout_seconds,
            },
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

    # Capture values before debit — the session may refresh key after commit
    pre_debit_balance = key.balance
    pre_debit_reserved = key.reserved_balance

    # --- DEBIT FIRST: atomically zero the balance before minting tokens ---
    # This prevents the race where a concurrent topup/spend happens between
    # reading the balance and minting the refund token (double-spend).
    debit_stmt = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key.hashed_key)
        .where(col(ApiKey.balance) == pre_debit_balance)
        .where(col(ApiKey.reserved_balance) == pre_debit_reserved)
        .values(balance=0, reserved_balance=0, reserved_at=None)
    )
    debit_result = await session.exec(debit_stmt)  # type: ignore[call-overload]
    await session.commit()

    if debit_result.rowcount == 0:
        # Balance changed between read and debit — another request is active
        raise HTTPException(
            status_code=409,
            detail="Balance changed concurrently. Please retry the refund.",
        )

    # The balance is locked at zero, so it is safe to create the refund token.
    effective_refund_mint = (
        key.refund_mint_url
        if key.refund_mint_url and key.refund_mint_url in settings.cashu_mints
        else settings.primary_mint
    )
    try:
        refund_currency = key.refund_currency or "sat"
        if key.refund_address:
            net_spent = await send_to_lnurl(
                remaining_balance,
                key.refund_currency or "sat",
                effective_refund_mint,
                key.refund_address,
            )
            result = {"recipient": key.refund_address}
            # The payout spends only the invoice amount while the debit
            # zeroed the full balance; credit the unused melt fee reserve
            # back as a separate write once the melt has confirmed.
            unused = remaining_balance - net_spent
            if unused > 0:
                credit_msat = (
                    unused * 1000
                    if (key.refund_currency or "sat") == "sat"
                    else unused
                )
                credit_stmt = (
                    update(ApiKey)
                    .where(col(ApiKey.hashed_key) == key.hashed_key)
                    .values(balance=col(ApiKey.balance) + credit_msat)
                )
                await session.exec(credit_stmt)  # type: ignore[call-overload]
                await session.commit()
        else:
            token = await send_token(
                remaining_balance, refund_currency, effective_refund_mint
            )
            effective_refund_mint = token_mint_url(token, effective_refund_mint)
            result = {"token": token}

        if key.refund_currency == "sat":
            result["sats"] = str(remaining_balance_msats // 1000)
        else:
            result["msats"] = str(remaining_balance_msats)

        if "token" in result:
            logger.info(
                "refund_wallet_endpoint: cashu token issued",
                extra={
                    "path": "/v1/wallet/refund",
                    "token_length": len(result["token"]),
                    "amount": remaining_balance,
                    "currency": key.refund_currency or "sat",
                },
            )

    except MeltOutcomeAmbiguousError as e:
        # The melt was dispatched and may still settle. Restoring the balance
        # here would let the same debit be paid out twice; keep the debit and
        # leave the outcome to reconciliation.
        logger.error(
            "refund_wallet_endpoint: melt outcome ambiguous; balance withheld "
            "pending reconciliation",
            extra={
                "error": str(e),
                "key_hash": key.hashed_key[:8],
                "remaining_balance": remaining_balance,
                "refund_currency": key.refund_currency,
                "refund_mint_url": key.refund_mint_url,
            },
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "Refund was dispatched but its outcome is unconfirmed; the "
                "balance is withheld until reconciliation completes"
            ),
        )
    except HTTPException:
        # Minting failed — restore the debited balance
        await _restore_balance(
            session,
            key.hashed_key,
            pre_debit_balance,
            pre_debit_reserved,
            key.refund_mint_url or "",
        )
        raise
    except Exception as e:
        # Minting failed — restore the debited balance
        await _restore_balance(
            session,
            key.hashed_key,
            pre_debit_balance,
            pre_debit_reserved,
            key.refund_mint_url or "",
        )
        error_msg = str(e)
        logger.error(
            "refund_wallet_endpoint: mint/send failed",
            extra={
                "error": error_msg,
                "error_type": type(e).__name__,
                "key_hash": key.hashed_key[:8],
                "remaining_balance": remaining_balance,
                "refund_currency": key.refund_currency,
                "refund_mint_url": key.refund_mint_url,
                "has_refund_address": bool(key.refund_address),
            },
        )
        if is_mint_connection_error(e):
            raise HTTPException(status_code=503, detail="Mint service unavailable")
        else:
            raise HTTPException(status_code=500, detail="Refund failed")

    await _refund_cache_set(bearer_value, result)

    if "token" in result:
        await store_cashu_transaction(
            token=result["token"],
            amount=remaining_balance,
            unit=key.refund_currency or "sat",
            mint_url=effective_refund_mint,
            typ="out",
            collected=False,
            source="apikey",
            api_key_hashed_key=key.hashed_key,
        )

    logger.info(
        "refund_wallet_endpoint: refund successful",
        extra={
            "refunded_msats": remaining_balance_msats,
            "previous_reserved_balance": key.reserved_balance,
        },
    )

    return result


@router.get("/history")
async def wallet_history(
    key: ApiKey = Depends(get_key_from_header),
    session: AsyncSession = Depends(get_session),
) -> dict[str, list[dict[str, str | int | bool | None]]]:
    result = await session.exec(
        select(CashuTransaction)
        .where(CashuTransaction.api_key_hashed_key == key.hashed_key)
        .order_by(col(CashuTransaction.created_at).desc())
    )
    transactions = result.all()
    return {
        "transactions": [
            {
                "id": tx.id,
                "type": tx.type,
                "source": tx.source,
                "amount": tx.amount,
                "unit": tx.unit,
                "mint_url": tx.mint_url,
                "created_at": tx.created_at,
                "collected": tx.collected,
                "swept": tx.swept,
            }
            for tx in transactions
        ]
    }


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


balance_router.include_router(lightning_router, include_in_schema=False)
balance_router.include_router(router)

deprecated_wallet_router = APIRouter(prefix="/v1/wallet", include_in_schema=False)
deprecated_wallet_router.include_router(router)
