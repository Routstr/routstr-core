"""Durable settlement state for X-Cashu payments.

The X-Cashu ``in`` row carries the payment's state, so a request that never
finished (restart, crash, cancellation) can be told apart from one that was
charged in full, and refunded later.

pending -> refunding -> refunded | failed
pending -> charged

Every X-Cashu path settles before the response starts, so a payment still
``pending`` after the request lifetime delivered nothing and is refunded in
full. ``refunding`` means a mint call may have produced a token; it is never
retried, because minting is not idempotent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Iterable
from typing import TypeVar

from sqlmodel import col, select, update

from ..core import get_logger
from ..core.db import (
    CashuTransaction,
    create_session,
    store_cashu_transaction_with_retry,
)
from ..core.settings import settings
from ..wallet import (
    recieve_token,
    send_token,
    token_mint_url,
    wallet_operation_guard,
)

logger = get_logger(__name__)

PENDING = "pending"
REFUNDING = "refunding"
REFUNDED = "refunded"
CHARGED = "charged"
FAILED = "failed"

RECOVERY_INTERVAL_SECONDS = 60
# Slack past the request deadline and its cleanup window before a payment is
# treated as abandoned.
RECOVERY_MARGIN_SECONDS = 60

T = TypeVar("T")

_settling: set[asyncio.Future[object]] = set()
# Requests holding a redeemed payment. Uvicorn cancels them at shutdown and
# starts lifespan shutdown without waiting for their refund handlers to run.
_paying: set[asyncio.Future[object]] = set()


def abandoned_after_seconds() -> float:
    return (
        settings.max_request_lifetime_seconds
        + settings.request_cleanup_timeout_seconds
        + RECOVERY_MARGIN_SECONDS
    )


async def _transition(request_id: str, from_states: Iterable[str], to: str) -> bool:
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(
                col(CashuTransaction.type) == "in",
                col(CashuTransaction.request_id) == request_id,
                col(CashuTransaction.settlement).in_(list(from_states)),
            )
            .values(settlement=to, settlement_at=int(time.time()))
        )
        await session.commit()
        return bool(result.rowcount)


async def _settlement_state(request_id: str) -> str | None:
    """The payment's state, or None when it is not tracked."""
    async with create_session() as session:
        result = await session.exec(
            select(CashuTransaction.settlement).where(
                CashuTransaction.type == "in",
                CashuTransaction.request_id == request_id,
                col(CashuTransaction.settlement).is_not(None),
            )
        )
        return result.first()


async def _has_stored_refund(request_id: str) -> bool:
    async with create_session() as session:
        result = await session.exec(
            select(CashuTransaction.id).where(
                CashuTransaction.type == "out",
                CashuTransaction.request_id == request_id,
            )
        )
        return result.first() is not None


async def claim_refund(request_id: str | None) -> bool:
    """Reserve the right to mint this payment's refund; at most one caller wins."""
    if request_id is None:
        return True
    if await _transition(request_id, (PENDING,), REFUNDING):
        return True
    # Payments without a tracked in row keep the behaviour from before tracking.
    return await _settlement_state(request_id) is None


async def finish_refund(request_id: str | None, refunded: bool) -> None:
    if request_id is None:
        return
    try:
        await _transition(request_id, (REFUNDING,), REFUNDED if refunded else FAILED)
    except Exception:
        # Recovery resolves a stale refunding row without minting again.
        logger.exception(
            "Failed to record X-Cashu refund outcome",
            extra={"request_id": request_id, "refunded": refunded},
        )


async def mark_charged(request_id: str | None) -> bool:
    """Record that the payment was kept in full, so recovery never refunds it.

    False means the charge is not durable, or recovery already took over the
    payment, so the response must not be delivered.
    """
    if request_id is None:
        return True
    for attempt in range(3):
        try:
            if await _transition(request_id, (PENDING,), CHARGED):
                return True
            # A retry finds the state an earlier, unacknowledged commit wrote.
            if await _settlement_state(request_id) in (None, CHARGED):
                return True
            logger.error(
                "X-Cashu payment was settled elsewhere before it was charged",
                extra={"request_id": request_id},
            )
            return False
        except Exception:
            if attempt == 2:
                logger.critical(
                    "Failed to record charged X-Cashu payment; withholding the "
                    "response, recovery will refund it",
                    extra={"request_id": request_id},
                    exc_info=True,
                )
                return False
            await asyncio.sleep(0.25 * 2**attempt)
    return False


def run_to_completion(work: Awaitable[T]) -> asyncio.Future[T]:
    """Start settlement work that request cancellation must not interrupt.

    Await the result through ``asyncio.shield``; shutdown drains what is left.
    """
    task = asyncio.ensure_future(work)
    _settling.add(task)  # type: ignore[arg-type]
    task.add_done_callback(_settling.discard)  # type: ignore[arg-type]
    # A cancelled caller may never read the outcome; the failure is logged.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return task


def payment_started() -> None:
    if (task := asyncio.current_task()) is not None:
        _paying.add(task)  # type: ignore[arg-type]


def payment_finished() -> None:
    if (task := asyncio.current_task()) is not None:
        _paying.discard(task)  # type: ignore[arg-type]


async def drain_settlements(timeout: float) -> None:
    """Let paid requests and their refunds settle before the loop stops."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    logged = False
    while pending := {task for task in _paying | _settling if not task.done()}:
        if not logged:
            logger.info(
                "Waiting for X-Cashu settlements", extra={"count": len(pending)}
            )
            logged = True
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.error(
                "X-Cashu settlements still running at shutdown; recovery resolves them",
                extra={"count": len(pending)},
            )
            return
        await asyncio.wait(
            pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )


async def _refund_abandoned(payment: CashuTransaction) -> bool:
    request_id = payment.request_id
    assert request_id is not None
    if not await _transition(request_id, (PENDING,), REFUNDING):
        return False
    try:
        token = await send_token(
            payment.amount, unit=payment.unit, mint_url=payment.mint_url
        )
    except Exception:
        logger.critical(
            "Failed to refund abandoned X-Cashu payment; manual reconciliation required",
            extra={
                "request_id": request_id,
                "amount": payment.amount,
                "unit": payment.unit,
                "mint": payment.mint_url,
            },
            exc_info=True,
        )
        await finish_refund(request_id, refunded=False)
        return False
    try:
        await store_cashu_transaction_with_retry(
            token=token,
            amount=payment.amount,
            unit=payment.unit,
            mint_url=token_mint_url(token, payment.mint_url),
            typ="out",
            request_id=request_id,
        )
    except Exception:
        # The write may have committed before failing to acknowledge it.
        try:
            stored = await _has_stored_refund(request_id)
        except Exception:
            logger.critical(
                "Could not tell whether the refund for an abandoned X-Cashu "
                "payment was stored; manual reconciliation required",
                extra={"request_id": request_id, "amount": payment.amount},
                exc_info=True,
            )
            return False
        if not stored:
            await _reclaim_unstored_refund(payment, token)
            return False
    await finish_refund(request_id, refunded=True)
    logger.warning(
        "Refunded abandoned X-Cashu payment",
        extra={
            "request_id": request_id,
            "amount": payment.amount,
            "unit": payment.unit,
            "age_seconds": int(time.time()) - payment.created_at,
        },
    )
    return True


async def _reclaim_unstored_refund(payment: CashuTransaction, token: str) -> None:
    """Take back a refund token that never left this process and retry later."""
    request_id = payment.request_id
    try:
        async with wallet_operation_guard():
            await recieve_token(token)
    except Exception:
        logger.critical(
            "Refund token for abandoned X-Cashu payment was minted but neither "
            "stored nor reclaimed; manual reconciliation required",
            extra={"request_id": request_id, "amount": payment.amount},
            exc_info=True,
        )
        await finish_refund(request_id, refunded=False)
        return
    logger.error(
        "Could not store the refund for an abandoned X-Cashu payment; "
        "reclaimed the token and will retry",
        extra={"request_id": request_id, "amount": payment.amount},
    )
    assert request_id is not None
    for attempt in range(3):
        try:
            if await _transition(request_id, (REFUNDING,), PENDING):
                return
            break
        except Exception:
            if attempt < 2:
                await asyncio.sleep(0.25 * 2**attempt)
    logger.critical(
        "Reclaimed an unstored X-Cashu refund but could not reopen the payment; "
        "the funds are in the node wallet, manual reconciliation required",
        extra={"request_id": request_id, "amount": payment.amount},
    )


async def _close_stale_refund(payment: CashuTransaction) -> None:
    request_id = payment.request_id
    assert request_id is not None
    stored = await _has_stored_refund(request_id)
    if not await _transition(request_id, (REFUNDING,), REFUNDED if stored else FAILED):
        return
    if not stored:
        logger.critical(
            "X-Cashu refund was interrupted after minting began; not retrying, "
            "manual reconciliation required",
            extra={
                "request_id": request_id,
                "amount": payment.amount,
                "unit": payment.unit,
                "mint": payment.mint_url,
            },
        )


async def recover_abandoned_payments() -> int:
    """Refund payments whose request ended without settling; returns the count."""
    cutoff = int(time.time() - abandoned_after_seconds())
    async with create_session() as session:
        result = await session.exec(
            select(CashuTransaction).where(
                CashuTransaction.type == "in",
                col(CashuTransaction.settlement).in_([PENDING, REFUNDING]),
                col(CashuTransaction.settlement_at) < cutoff,
                col(CashuTransaction.request_id).is_not(None),
            )
        )
        payments = result.all()

    refunded = 0
    for payment in payments:
        try:
            if payment.settlement == PENDING:
                # Shutdown must not interrupt a mint half way through.
                refunded += await asyncio.shield(
                    run_to_completion(_refund_abandoned(payment))
                )
            else:
                await _close_stale_refund(payment)
        except Exception:
            logger.exception(
                "Error recovering X-Cashu payment",
                extra={"request_id": payment.request_id},
            )
    return refunded


async def periodic_x_cashu_recovery() -> None:
    while True:
        try:
            await recover_abandoned_payments()
        except Exception:
            logger.exception("Error in periodic_x_cashu_recovery")
        await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
