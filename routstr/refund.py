"""Durable, mutually exclusive refund claims for API key balances.

A claim debits the balance and records the payout in one transaction, so a key
can never have two payouts in flight and no crash can leave a debited balance
without a record of why.
"""

import asyncio
import time

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
    """Persisted mint preferences must not outlive the trusted-mint config."""
    if key.refund_mint_url and key.refund_mint_url in settings.cashu_mints:
        return key.refund_mint_url
    return settings.primary_mint


async def validate_lightning_destination(destination: str) -> None:
    """Resolve the destination before claiming, so a bad address never debits."""
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
    """Debit the balance to zero and record the claim in one transaction.

    The claim starts leased (``claimed_at``) to the request that opened it, so
    the reconciler leaves it alone until ``refund_claim_timeout_seconds`` have
    passed; a payout still in flight is never released underneath itself.
    """
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
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "message": "A refund for this key is already in progress.",
                    "type": "invalid_request_error",
                    "code": "refund_in_progress",
                }
            },
        )
    return refund


async def _close(session: AsyncSession, refund: Refund, **values: object) -> bool:
    result = await session.exec(  # type: ignore[call-overload]
        update(Refund)
        .where(col(Refund.id) == refund.id)
        .where(col(Refund.status).in_(REFUND_OPEN_STATUSES))
        .values(claimed_at=None, updated_at=int(time.time()), **values)
    )
    return bool(result.rowcount)


async def record_quote(refund: Refund, quote_id: str) -> None:
    """Persist the melt quote before the melt is dispatched.

    Once the quote is on disk the reconciler can ask the mint what became of
    it, so a crash after this point can never be mistaken for "never sent".
    Raises if the claim is no longer open, which aborts the payout.
    """
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(Refund)
            .where(col(Refund.id) == refund.id)
            .where(col(Refund.status).in_(REFUND_OPEN_STATUSES))
            .values(quote_id=quote_id, updated_at=int(time.time()))
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
    values: dict[str, object] = {"status": "paid"}
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


async def release(session: AsyncSession, refund: Refund) -> bool:
    """Close the claim and return the debited balance in the same transaction."""
    if not await _close(session, refund, status="failed"):
        # Nothing changed; commit rather than roll back so the session stays
        # usable (an async rollback after an ORM-enabled UPDATE expires the
        # identity map and later loads fail outside the greenlet).
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
    """Keep the debit and the claim open until reconciliation resolves it."""
    await _close(session, refund, status="ambiguous", quote_id=quote_id)
    await session.commit()


async def latest_terminal(session: AsyncSession, key: ApiKey) -> Refund | None:
    """Most recent paid Lightning refund, for idempotent re-requests.

    Cashu payouts are deliberately excluded: their token lives in
    ``cashu_transactions`` whose ``collected``/``swept`` flags decide whether
    it may still be handed out.
    """
    result = await session.exec(
        select(Refund)
        .where(Refund.api_key_hashed_key == key.hashed_key)
        .where(Refund.status == "paid")
        .where(Refund.method == "lightning")
        .order_by(col(Refund.created_at).desc())
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


async def execute(session: AsyncSession, refund: Refund) -> dict[str, str]:
    """Pay out an open claim, closing it on every outcome the mint makes known."""
    amount = amount_in_unit(refund.amount_msats, refund.unit)
    quote_id: str | None = None

    async def capture_quote(quote: str) -> None:
        nonlocal quote_id
        quote_id = quote
        await record_quote(refund, quote)

    try:
        if refund.method == "lightning":
            await send_to_lnurl(
                amount,
                refund.unit,
                refund.mint_url,
                str(refund.destination),
                on_melt_quote=capture_quote,
            )
            await settle(session, refund, quote_id=quote_id)
        else:
            token = await send_token(amount, refund.unit, refund.mint_url)
            mint_url = token_mint_url(token, refund.mint_url)
            await settle(session, refund, token=token, mint_url=mint_url)
            await store_cashu_transaction(
                token=token,
                amount=amount,
                unit=refund.unit,
                mint_url=mint_url,
                typ="out",
                collected=False,
                source="apikey",
                api_key_hashed_key=refund.api_key_hashed_key,
            )
            refund.token = token
            refund.mint_url = mint_url
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


async def _reconcile(refund: Refund) -> None:
    if refund.method != "lightning":
        # A cashu payout leaves no quote to query: the token either reached the
        # client or was lost with the process. Close the claim as ``stuck`` so
        # the balance stays withheld and the operator is told exactly once.
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
        # No melt quote exists, so the mint was never asked to pay.
        async with create_session() as session:
            await release(session, refund)
        return

    status = await check_bolt11_payment_status(
        refund.mint_url, refund.unit, refund.quote_id
    )
    if status == "paid":
        async with create_session() as session:
            await settle(session, refund)
    elif status == "unpaid":
        async with create_session() as session:
            await release(session, refund)
    else:
        logger.warning(
            "refund still unresolved at the mint",
            extra={"refund_id": refund.id, "melt_status": status},
        )


async def reconcile_once() -> None:
    """Resolve open claims whose lease has lapsed.

    A fresh claim is leased to the request paying it out; ``hold`` drops that
    lease so an ambiguous outcome is queried on the next pass.
    """
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
            await _reconcile(refund)
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
