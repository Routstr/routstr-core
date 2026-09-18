"""Refund claims: one open payout per API key, recorded before it is paid."""

import asyncio
import time
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, func, select, update

from .core.db import (
    REFUND_OPEN_STATUSES,
    REFUND_UNRESOLVED_STATUSES,
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
from .payment.lnurl import (
    LNURLError,
    MeltOutcomeAmbiguousError,
    MeltUnpaidError,
    get_lnurl_data,
)
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
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=400, detail=f"Lightning destination unreachable: {e}"
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
    # created_at has second resolution; step past the previous claim so the
    # newest claim for a key always sorts first.
    latest = await session.exec(
        select(func.max(col(Refund.created_at))).where(
            Refund.api_key_hashed_key == key.hashed_key
        )
    )
    created_at = max(int(time.time()), (latest.one() or 0) + 1)
    refund = Refund(
        api_key_hashed_key=key.hashed_key,
        method=method,
        destination=destination,
        amount_msats=key.total_balance,
        unit=unit,
        mint_url=refund_mint(key),
        claimed_at=int(time.time()),
        created_at=created_at,
        updated_at=created_at,
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
    require_no_token: bool = False,
    from_statuses: tuple[str, ...] = REFUND_OPEN_STATUSES,
    **values: object,
) -> bool:
    stmt = (
        update(Refund)
        .where(col(Refund.id) == refund.id)
        .where(col(Refund.status).in_(from_statuses))
    )
    if require_no_quote:
        # A quote recorded since the row was read means a melt may be in flight.
        stmt = stmt.where(col(Refund.quote_id).is_(None))
    if require_no_token:
        stmt = stmt.where(col(Refund.token).is_(None))
    result = await session.exec(  # type: ignore[call-overload]
        stmt.values(claimed_at=None, updated_at=int(time.time()), **values)
    )
    return bool(result.rowcount)


async def renew_lease(session: AsyncSession, refund: Refund) -> None:
    """Push the reconciler lease forward before a slow mint step."""
    await session.exec(  # type: ignore[call-overload]
        update(Refund)
        .where(col(Refund.id) == refund.id)
        .where(col(Refund.status).in_(REFUND_OPEN_STATUSES))
        .values(claimed_at=int(time.time()))
    )
    await session.commit()


async def record_quote(refund: Refund, quote_id: str, mint_url: str) -> None:
    """Store the quote and its mint before the melt is sent; raises if the claim closed.

    Mint fallback can issue the quote on a different mint than the claim's.
    """
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(Refund)
            .where(col(Refund.id) == refund.id)
            .where(col(Refund.status).in_(REFUND_OPEN_STATUSES))
            # Renew the lease so the reconciler leaves the payout alone.
            .values(
                quote_id=quote_id,
                mint_url=mint_url,
                claimed_at=int(time.time()),
                updated_at=int(time.time()),
            )
        )
        await session.commit()
    if not result.rowcount:
        raise LNURLError("Refund claim closed before the melt was dispatched")
    refund.quote_id = quote_id
    refund.mint_url = mint_url


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
    # The payout side knows the money moved, so a claim the reconciler gave up
    # on (stuck) is closed as paid too.
    settled = await _close(
        session, refund, from_statuses=REFUND_UNRESOLVED_STATUSES, **values
    )
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


async def hold(
    session: AsyncSession,
    refund: Refund,
    quote_id: str | None,
    *,
    token: str | None = None,
) -> None:
    """Withhold the balance; the quote or token names what the mint may have paid."""
    values: dict[str, Any] = {"status": "ambiguous", "quote_id": quote_id}
    if token is not None:
        values["token"] = token
        values["mint_url"] = refund.mint_url
    await _close(session, refund, **values)
    await session.commit()


def refund_in_progress_error(refund: Refund | None = None) -> HTTPException:
    """The 409 raised when a key already has an unresolved refund claim."""
    stuck = refund is not None and refund.status == "stuck"
    error: dict[str, str] = {
        "message": (
            "A refund for this key is unresolved and requires operator reconciliation."
            if stuck
            else "A refund for this key is already in progress."
        ),
        "type": "invalid_request_error",
        "code": "refund_unresolved" if stuck else "refund_in_progress",
    }
    if refund is not None:
        error["refund_id"] = refund.id
        error["status"] = refund.status
    return HTTPException(status_code=409, detail={"error": error})


async def _latest_with_status(
    session: AsyncSession, key: ApiKey, statuses: tuple[str, ...]
) -> Refund | None:
    result = await session.exec(
        select(Refund)
        .where(Refund.api_key_hashed_key == key.hashed_key)
        .where(col(Refund.status).in_(statuses))
        .order_by(col(Refund.created_at).desc(), col(Refund.updated_at).desc())
    )
    return result.first()


async def latest_open(session: AsyncSession, key: ApiKey) -> Refund | None:
    """Latest non-terminal (in-flight) claim for the key, if any.

    An open claim means a prior refund already debited the balance and is still
    settling, so the balance reads as zero even though a refund is under way.
    """
    return await _latest_with_status(session, key, REFUND_OPEN_STATUSES)


async def latest_stuck(session: AsyncSession, key: ApiKey) -> Refund | None:
    """Latest claim the reconciler gave up on; needs operator recovery."""
    return await _latest_with_status(session, key, ("stuck",))


async def latest_terminal(session: AsyncSession, key: ApiKey) -> Refund | None:
    """Latest paid refund of either method.

    Cashu tokens are normally served from cashu_transactions, which tracks
    collection and sweeping; the claim row is the fallback when that ledger
    write failed after the token was already issued.
    """
    return await _latest_with_status(session, key, ("paid",))


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


async def _pay_lightning(session: AsyncSession, refund: Refund) -> bool:
    async def capture_quote(quote: str, mint_url: str) -> None:
        await record_quote(refund, quote, mint_url)

    try:
        await send_to_lnurl(
            amount_in_unit(refund.amount_msats, refund.unit),
            refund.unit,
            refund.mint_url,
            str(refund.destination),
            on_melt_quote=capture_quote,
        )
    except MeltOutcomeAmbiguousError as e:
        await hold(session, refund, refund.quote_id)
        logger.error(
            "refund outcome ambiguous; balance withheld pending reconciliation",
            extra={
                "refund_id": refund.id,
                "error": str(e),
                "key_hash": refund.api_key_hashed_key[:8],
                "quote_id": refund.quote_id,
            },
        )
        raise
    return await settle(session, refund, quote_id=refund.quote_id)


async def _pay_cashu(session: AsyncSession, refund: Refund) -> bool:
    amount = amount_in_unit(refund.amount_msats, refund.unit)
    await renew_lease(session, refund)
    token = await send_token(amount, refund.unit, refund.mint_url)
    # From here the token is bearer money: keep it on the claim so a failed
    # settle withholds the balance instead of restoring it.
    refund.token = token
    refund.mint_url = token_mint_url(token, refund.mint_url)
    return await settle(session, refund, token=token, mint_url=refund.mint_url)


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


def unresolved_refund_error() -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=(
            "Refund was dispatched but its outcome is unconfirmed; the "
            "balance is withheld until reconciliation completes"
        ),
    )


async def _abort(session: AsyncSession, refund: Refund) -> None:
    """Fail the claim, or withhold it once a melt quote or token exists.

    A recorded quote means the mint may already have paid; an issued token is
    already bearer money. In both cases the balance must not be restored.
    """
    if refund.quote_id is None and refund.token is None:
        await release(session, refund)
        return
    await hold(session, refund, refund.quote_id, token=refund.token)
    logger.error(
        "refund failed after its payout was dispatched; balance withheld "
        "pending reconciliation",
        extra={
            "refund_id": refund.id,
            "key_hash": refund.api_key_hashed_key[:8],
            "quote_id": refund.quote_id,
            "has_token": refund.token is not None,
            "mint_url": refund.mint_url,
        },
    )
    raise unresolved_refund_error()


async def execute(session: AsyncSession, refund: Refund) -> dict[str, str]:
    attached_refund = refund
    # Keep payout evidence outside the identity map: a failed flush/commit can
    # expire attached attributes, including the only copy of an issued token.
    refund = Refund(**refund.model_dump())
    try:
        if refund.method == "lightning":
            settled = await _pay_lightning(session, refund)
        else:
            settled = await _pay_cashu(session, refund)
    except MeltOutcomeAmbiguousError:
        # Already held by _pay_lightning; releasing here would pay out twice.
        raise unresolved_refund_error()
    except MeltUnpaidError as e:
        # The mint answered the melt itself with unpaid: proof that nothing was
        # sent, so the balance goes back now rather than after reconciliation.
        await release(session, refund)
        logger.warning(
            "refund melt unpaid at the mint; balance restored",
            extra={
                "refund_id": refund.id,
                "error": str(e),
                "key_hash": refund.api_key_hashed_key[:8],
                "quote_id": refund.quote_id,
            },
        )
        raise HTTPException(
            status_code=503,
            detail="Lightning payment failed at the mint; balance restored. Retry later.",
        )
    except HTTPException:
        await session.rollback()
        await _abort(session, refund)
        raise
    except Exception as e:
        await session.rollback()
        await _abort(session, refund)
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

    if settled:
        refund.status = "paid"
        refund.claimed_at = None
    else:
        # Report the row as it stands rather than a status that was not written.
        await session.refresh(attached_refund)
        refund = attached_refund
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
        if refund.token is not None:
            # The token was issued and kept on the claim; the payout is done.
            async with create_session() as session:
                await settle(session, refund)
            return
        # No quote to query for cashu; withhold the balance and alert once.
        async with create_session() as session:
            if await _close(session, refund, require_no_token=True, status="stuck"):
                await session.commit()
                logger.critical(
                    "cashu refund stuck; balance withheld, manual reconciliation required",
                    extra={
                        "refund_id": refund.id,
                        "key_hash": refund.api_key_hashed_key[:8],
                        "amount_msats": refund.amount_msats,
                    },
                )
            else:
                await session.commit()
                current = await session.get(Refund, refund.id)
                if current is not None and current.token is not None:
                    await settle(session, current)
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
