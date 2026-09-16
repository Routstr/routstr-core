"""Refund claims: one open payout per API key, recorded before it is paid."""

import asyncio
import time
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select, update

from .core.db import (
    REFUND_OPEN_STATUSES,
    ApiKey,
    AsyncSession,
    Refund,
    create_session,
)
from .core.db import (
    store_cashu_transaction_with_retry as store_cashu_transaction,
)
from .core.logging import get_logger
from .core.settings import settings
from .payment.lnurl import LNURLError, MeltOutcomeAmbiguousError, get_lnurl_data
from .wallet import (
    check_bolt11_payment_status,
    is_mint_connection_error,
    send_to_lnurl,
    send_token,
    token_mint_url,
)

logger = get_logger(__name__)

RECONCILE_BATCH_LIMIT = 100


def refund_unit(key: ApiKey) -> str:
    return key.refund_currency or "sat"


def amount_in_unit(amount_msats: int, unit: str) -> int:
    return amount_msats // 1000 if unit == "sat" else amount_msats


def refund_mint(key: ApiKey) -> str:
    if key.refund_mint_url and key.refund_mint_url in settings.cashu_mints:
        return key.refund_mint_url
    return settings.primary_mint


async def validate_lightning_destination(destination: str) -> None:
    try:
        await get_lnurl_data(destination)
    except LNURLError as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid lightning destination: {e}"
        )


async def open_claim(
    session: AsyncSession,
    key: ApiKey,
    *,
    method: str,
    destination: str | None,
) -> Refund:
    """Zero the balance and insert the claim in one transaction."""
    unit = refund_unit(key)
    refund = Refund(
        api_key_hashed_key=key.hashed_key,
        method=method,
        destination=destination,
        amount_msats=key.total_balance,
        unit=unit,
        mint_url=refund_mint(key),
        claimed_at=int(time.time()),
    )
    debit = (
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key.hashed_key)
        .where(col(ApiKey.balance) == key.balance)
        .where(col(ApiKey.reserved_balance) == key.reserved_balance)
        .values(balance=0, reserved_balance=0, reserved_at=None)
    )
    try:
        debited = await session.exec(debit)  # type: ignore[call-overload]
        if debited.rowcount == 0:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail="Balance changed concurrently. Please retry the refund.",
            )
        session.add(refund)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise refund_in_progress_error()
    return refund


async def _close(
    session: AsyncSession,
    refund: Refund,
    *,
    require_no_quote: bool = False,
    **values: object,
) -> bool:
    stmt = (
        update(Refund)
        .where(col(Refund.id) == refund.id)
        .where(col(Refund.status).in_(REFUND_OPEN_STATUSES))
    )
    if require_no_quote:
        # A quote recorded since the row was read means a melt may be in flight.
        stmt = stmt.where(col(Refund.quote_id).is_(None))
    result = await session.exec(  # type: ignore[call-overload]
        stmt.values(claimed_at=None, updated_at=int(time.time()), **values)
    )
    return bool(result.rowcount)


async def record_quote(refund: Refund, quote_id: str) -> None:
    """Store the melt quote before the melt is sent; raises if the claim closed."""
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(Refund)
            .where(col(Refund.id) == refund.id)
            .where(col(Refund.status).in_(REFUND_OPEN_STATUSES))
            # Renew the lease so the reconciler leaves the payout alone.
            .values(
                quote_id=quote_id,
                claimed_at=int(time.time()),
                updated_at=int(time.time()),
            )
        )
        await session.commit()
    if not result.rowcount:
        raise LNURLError("Refund claim closed before the melt was dispatched")
    refund.quote_id = quote_id


async def settle(
    session: AsyncSession,
    refund: Refund,
    *,
    quote_id: str | None = None,
    token: str | None = None,
    mint_url: str | None = None,
) -> bool:
    values: dict[str, Any] = {"status": "paid"}
    if quote_id is not None:
        values["quote_id"] = quote_id
    if token is not None:
        values["token"] = token
    if mint_url is not None:
        values["mint_url"] = mint_url
    settled = await _close(session, refund, **values)
    await session.commit()
    if not settled:
        logger.warning(
            "refund paid but its claim was already closed",
            extra={"refund_id": refund.id, "prior_status": refund.status},
        )
    return settled


async def release(
    session: AsyncSession, refund: Refund, *, require_no_quote: bool = False
) -> bool:
    """Mark the claim failed and restore the balance."""
    if not await _close(
        session, refund, require_no_quote=require_no_quote, status="failed"
    ):
        # Commit, not rollback: rollback after an ORM UPDATE breaks later loads.
        await session.commit()
        return False
    await session.exec(  # type: ignore[call-overload]
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == refund.api_key_hashed_key)
        .values(balance=col(ApiKey.balance) + refund.amount_msats)
    )
    await session.commit()
    logger.info(
        "refund released; balance restored",
        extra={
            "refund_id": refund.id,
            "key_hash": refund.api_key_hashed_key[:8],
            "restored_msats": refund.amount_msats,
        },
    )
    return True


async def hold(session: AsyncSession, refund: Refund, quote_id: str | None) -> None:
    await _close(session, refund, status="ambiguous", quote_id=quote_id)
    await session.commit()


def refund_in_progress_error() -> HTTPException:
    """The 409 raised when a key already has an in-flight refund claim."""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "message": "A refund for this key is already in progress.",
                "type": "invalid_request_error",
                "code": "refund_in_progress",
            }
        },
    )


async def latest_open(session: AsyncSession, key: ApiKey) -> Refund | None:
    """Latest non-terminal (in-flight) claim for the key, if any.

    An open claim means a prior refund already debited the balance and is still
    settling, so the balance reads as zero even though a refund is under way.
    """
    result = await session.exec(
        select(Refund)
        .where(Refund.api_key_hashed_key == key.hashed_key)
        .where(col(Refund.status).in_(REFUND_OPEN_STATUSES))
        .order_by(col(Refund.created_at).desc(), col(Refund.updated_at).desc())
    )
    return result.first()


async def latest_terminal(session: AsyncSession, key: ApiKey) -> Refund | None:
    """Latest paid refund of either method.

    Cashu tokens are normally served from cashu_transactions, which tracks
    collection and sweeping; the claim row is the fallback when that ledger
    write failed after the token was already issued.
    """
    result = await session.exec(
        select(Refund)
        .where(Refund.api_key_hashed_key == key.hashed_key)
        .where(Refund.status == "paid")
        .order_by(col(Refund.created_at).desc(), col(Refund.updated_at).desc())
    )
    return result.first()


def describe(refund: Refund) -> dict[str, str]:
    body: dict[str, str] = {"refund_id": refund.id, "status": refund.status}
    if refund.token:
        body["token"] = refund.token
    if refund.destination:
        body["recipient"] = refund.destination
    if refund.unit == "sat":
        body["sats"] = str(refund.amount_msats // 1000)
    else:
        body["msats"] = str(refund.amount_msats)
    return body


async def _pay_lightning(session: AsyncSession, refund: Refund) -> None:
    quote_id: str | None = None

    async def capture_quote(quote: str) -> None:
        nonlocal quote_id
        quote_id = quote
        await record_quote(refund, quote)

    try:
        await send_to_lnurl(
            amount_in_unit(refund.amount_msats, refund.unit),
            refund.unit,
            refund.mint_url,
            str(refund.destination),
            on_melt_quote=capture_quote,
        )
    except MeltOutcomeAmbiguousError as e:
        await hold(session, refund, quote_id)
        logger.error(
            "refund outcome ambiguous; balance withheld pending reconciliation",
            extra={
                "refund_id": refund.id,
                "error": str(e),
                "key_hash": refund.api_key_hashed_key[:8],
                "quote_id": quote_id,
            },
        )
        raise
    await settle(session, refund, quote_id=quote_id)


async def _pay_cashu(session: AsyncSession, refund: Refund) -> None:
    amount = amount_in_unit(refund.amount_msats, refund.unit)
    token = await send_token(amount, refund.unit, refund.mint_url)
    mint_url = token_mint_url(token, refund.mint_url)
    await settle(session, refund, token=token, mint_url=mint_url)
    refund.token = token
    refund.mint_url = mint_url


async def _record_cashu_payout(refund: Refund) -> None:
    """Ledger write for an issued token; the claim row already holds the token,
    so a failure here must not fail the request or release the balance."""
    try:
        await store_cashu_transaction(
            token=str(refund.token),
            amount=amount_in_unit(refund.amount_msats, refund.unit),
            unit=refund.unit,
            mint_url=refund.mint_url,
            typ="out",
            collected=False,
            source="apikey",
            api_key_hashed_key=refund.api_key_hashed_key,
        )
    except Exception as e:
        logger.error(
            "refund token issued but cashu transaction was not recorded",
            extra={
                "refund_id": refund.id,
                "error": str(e),
                "error_type": type(e).__name__,
                "key_hash": refund.api_key_hashed_key[:8],
            },
        )


async def execute(session: AsyncSession, refund: Refund) -> dict[str, str]:
    try:
        if refund.method == "lightning":
            await _pay_lightning(session, refund)
        else:
            await _pay_cashu(session, refund)
    except MeltOutcomeAmbiguousError:
        # Already held by _pay_lightning; releasing here would pay out twice.
        raise HTTPException(
            status_code=502,
            detail=(
                "Refund was dispatched but its outcome is unconfirmed; the "
                "balance is withheld until reconciliation completes"
            ),
        )
    except HTTPException:
        await release(session, refund)
        raise
    except Exception as e:
        await release(session, refund)
        logger.error(
            "refund payout failed",
            extra={
                "refund_id": refund.id,
                "error": str(e),
                "error_type": type(e).__name__,
                "key_hash": refund.api_key_hashed_key[:8],
                "method": refund.method,
                "mint_url": refund.mint_url,
            },
        )
        if is_mint_connection_error(e):
            raise HTTPException(status_code=503, detail="Mint service unavailable")
        raise HTTPException(status_code=500, detail="Refund failed")

    if refund.method == "cashu":
        await _record_cashu_payout(refund)

    refund.status = "paid"
    refund.claimed_at = None
    logger.info(
        "refund paid",
        extra={
            "refund_id": refund.id,
            "method": refund.method,
            "amount_msats": refund.amount_msats,
            "key_hash": refund.api_key_hashed_key[:8],
        },
    )
    return describe(refund)


async def _lease(refund_id: str, now: int, lease_cutoff: int) -> bool:
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(Refund)
            .where(col(Refund.id) == refund_id)
            .where(col(Refund.status).in_(REFUND_OPEN_STATUSES))
            .where(
                col(Refund.claimed_at).is_(None)
                | (col(Refund.claimed_at) < lease_cutoff)
            )
            .values(claimed_at=now)
        )
        await session.commit()
        return bool(result.rowcount)


async def _reconcile(refund: Refund, now: int) -> None:
    if refund.method != "lightning":
        # No quote to query for cashu; withhold the balance and alert once.
        async with create_session() as session:
            if await _close(session, refund, status="stuck"):
                await session.commit()
                logger.critical(
                    "cashu refund stuck; balance withheld, manual reconciliation required",
                    extra={
                        "refund_id": refund.id,
                        "key_hash": refund.api_key_hashed_key[:8],
                        "amount_msats": refund.amount_msats,
                    },
                )
        return

    if refund.quote_id is None:
        # Never sent, unless a quote appeared since the row was read.
        async with create_session() as session:
            if not await release(session, refund, require_no_quote=True):
                logger.info(
                    "refund gained a melt quote during reconciliation; left open",
                    extra={"refund_id": refund.id},
                )
        return

    status = await check_bolt11_payment_status(
        refund.mint_url, refund.unit, refund.quote_id
    )
    if status == "paid":
        async with create_session() as session:
            await settle(session, refund)
    elif status == "unpaid":
        # A fresh melt can report unpaid briefly; trust it only after a timeout.
        if refund.updated_at > now - settings.refund_claim_timeout_seconds:
            logger.info(
                "refund unpaid at the mint but too recent to release; waiting",
                extra={"refund_id": refund.id, "updated_at": refund.updated_at},
            )
            return
        async with create_session() as session:
            await release(session, refund)
    else:
        logger.warning(
            "refund still unresolved at the mint",
            extra={"refund_id": refund.id, "melt_status": status},
        )


async def reconcile_once() -> None:
    """Resolve open claims whose lease has lapsed."""
    now = int(time.time())
    lease_cutoff = now - settings.refund_claim_timeout_seconds
    async with create_session() as session:
        result = await session.exec(
            select(Refund)
            .where(col(Refund.status).in_(REFUND_OPEN_STATUSES))
            .where(
                col(Refund.claimed_at).is_(None)
                | (col(Refund.claimed_at) < lease_cutoff)
            )
            .order_by(col(Refund.created_at))
            .limit(RECONCILE_BATCH_LIMIT)
        )
        stale = list(result.all())

    for refund in stale:
        if not await _lease(refund.id, now, lease_cutoff):
            continue
        try:
            await _reconcile(refund, now)
        except Exception as e:
            logger.error(
                "refund reconciliation failed",
                extra={
                    "refund_id": refund.id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )


async def periodic_refund_reconcile() -> None:
    while True:
        await asyncio.sleep(settings.refund_reconcile_interval_seconds)
        try:
            await reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "refund reconcile loop error",
                extra={"error": str(e), "error_type": type(e).__name__},
            )
