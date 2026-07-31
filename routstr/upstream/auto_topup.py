import asyncio
import json
import math
import time
import typing
import uuid

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, or_, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from ..core import get_logger
from ..core.db import (
    CashuTransaction,
    UpstreamProviderRow,
    create_session,
)
from ..core.db import (
    store_cashu_transaction_with_retry as store_cashu_transaction,
)
from ..payment.price import sats_usd_price
from ..wallet import (
    Bolt11PaymentAmbiguous,
    Bolt11PaymentNotAttempted,
    check_bolt11_payment_status,
    execute_bolt11_payment,
    maximum_owner_cashu_balance_sats,
    prepare_bolt11_payment,
    send_token,
)
from .ppqai import PPQAIUpstreamProvider
from .routstr import RoutstrUpstreamProvider

logger = get_logger(__name__)

# Check every 60 seconds
AUTO_TOPUP_INTERVAL_SECONDS = 60
# Claim lifecycle. "claimed" holds the slot while the invoice is being created
# and priced; nothing has been spent yet, so it is always safe to release.
# "in_flight" means proofs are committed to a mint and the outcome is unknown.
# "reconcile" means the worker gave up and an admin must decide.
PPQ_PHASE_CLAIMED = "claimed"
PPQ_PHASE_IN_FLIGHT = "in_flight"
PPQ_PHASE_RECONCILE = "reconcile"
PPQ_PHASES = frozenset({PPQ_PHASE_CLAIMED, PPQ_PHASE_IN_FLIGHT, PPQ_PHASE_RECONCILE})
PPQ_SETTLEMENT_ATTEMPTS = 5
PPQ_SETTLEMENT_POLL_SECONDS = 2
PPQ_PENDING_TTL_SECONDS = 15 * 60
PPQ_MAX_INVOICE_PREMIUM = 1.10
PPQ_MIN_TOPUP_USD = 1
PPQ_MAX_TOPUP_USD = 500


async def periodic_auto_topup() -> None:
    """Background task that monitors Routstr and PPQ provider balances."""
    # Wait for initial startup to complete
    await asyncio.sleep(30)
    logger.info("Auto top-up worker started")

    while True:
        try:
            await _run_auto_topup_cycle()
        except Exception as e:
            logger.error(
                "Auto top-up cycle failed",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

        await asyncio.sleep(AUTO_TOPUP_INTERVAL_SECONDS)


async def _run_auto_topup_cycle() -> None:
    """Single cycle: check all eligible providers and top up if needed."""
    reconciled_ppq_provider_ids = await _reconcile_all_ppq_claims()

    async with create_session() as session:
        query = select(UpstreamProviderRow).where(
            col(UpstreamProviderRow.provider_type).in_(["routstr", "ppqai"]),
            UpstreamProviderRow.enabled == True,  # noqa: E712
        )
        result = await session.exec(query)
        providers = result.all()

    for row in providers:
        # Do not immediately retry a PPQ claim reconciled this cycle: PPQ's
        # balance endpoint may lag its invoice status and cause a duplicate.
        if row.id is not None and row.id in reconciled_ppq_provider_ids:
            continue
        try:
            await _check_and_topup(row)
        except Exception as e:
            logger.error(
                "Auto top-up failed for provider",
                extra={
                    "provider_id": row.id,
                    "base_url": row.base_url,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )


async def _reconcile_all_ppq_claims() -> set[int]:
    """Reconcile active PPQ claims and return providers suppressed this cycle."""
    async with create_session() as session:
        rows = (
            await session.exec(
                select(UpstreamProviderRow).where(
                    col(UpstreamProviderRow.provider_type) == "ppqai"
                )
            )
        ).all()

    active_provider_ids: set[int] = set()
    for row in rows:
        try:
            if row.id is None:
                continue
            state = await get_ppq_auto_topup_state(row.id)
            if not state.get("active"):
                continue
            active_provider_ids.add(row.id)
            provider = PPQAIUpstreamProvider.from_db_row(row) if row.api_key else None
            await _reconcile_ppq_state(row, provider)
        except Exception as e:
            logger.error(
                "PPQ claim reconciliation failed",
                extra={"provider_id": row.id, "error": str(e)},
            )
    return active_provider_ids


def _invalid_ppq_number(value: object, *, integer: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    try:
        number = float(value)
    except OverflowError:
        return True
    return (
        not math.isfinite(number)
        or number <= 0
        or (integer and not number.is_integer())
    )


def validate_ppq_auto_topup_settings(settings: dict | None) -> str | None:
    """Return why enabled PPQ auto top-up settings are invalid, if anything."""
    if not settings or not settings.get("auto_topup"):
        return None

    threshold = settings.get("topup_threshold")
    amount = settings.get("topup_amount_limit")
    if _invalid_ppq_number(threshold):
        return "PPQ auto top-up threshold must be a positive number"
    if _invalid_ppq_number(amount, integer=True):
        return "PPQ auto top-up amount must be a positive whole number"
    amount_usd = int(typing.cast(int | float, amount))
    if not PPQ_MIN_TOPUP_USD <= amount_usd <= PPQ_MAX_TOPUP_USD:
        return (
            f"PPQ auto top-up amount must be between {PPQ_MIN_TOPUP_USD} "
            f"and {PPQ_MAX_TOPUP_USD} USD"
        )
    return None


async def _check_and_topup_ppq_from_row(row: UpstreamProviderRow) -> None:
    settings: dict = {}
    if row.provider_settings:
        try:
            settings = json.loads(row.provider_settings)
        except (json.JSONDecodeError, TypeError):
            return
    if not isinstance(settings, dict) or not settings.get("auto_topup"):
        return

    problem = validate_ppq_auto_topup_settings(settings)
    if problem is not None:
        logger.warning(
            "PPQ auto top-up enabled but its configuration is invalid",
            extra={"provider_id": row.id, "problem": problem},
        )
        return
    if not row.api_key:
        return
    await _check_and_topup_ppq(row, settings)


async def _check_and_topup(row: UpstreamProviderRow) -> None:
    """Check a single provider's balance and top up if below threshold."""
    if row.provider_type == "ppqai":
        await _check_and_topup_ppq_from_row(row)
        return

    # Parse provider settings
    settings: dict = {}
    if row.provider_settings:
        try:
            settings = json.loads(row.provider_settings)
        except (json.JSONDecodeError, TypeError):
            return

    if not settings.get("auto_topup"):
        return

    threshold = settings.get("topup_threshold")
    amount = settings.get("topup_amount_limit")
    mint_url = settings.get("topup_mint_url")

    if not threshold or not amount or not mint_url:
        logger.warning(
            "Auto top-up enabled but missing configuration",
            extra={
                "provider_id": row.id,
                "has_threshold": bool(threshold),
                "has_amount": bool(amount),
                "has_mint": bool(mint_url),
            },
        )
        return

    if not row.api_key:
        return

    # Instantiate provider and check balance
    provider = RoutstrUpstreamProvider.from_db_row(row)
    if provider is None:
        return
    balance = await provider.get_balance()

    if balance is None:
        logger.warning(
            "Could not fetch balance for auto top-up",
            extra={"provider_id": row.id, "base_url": row.base_url},
        )
        return

    if balance >= threshold * 1000:
        return

    # Balance is below threshold - create token and top up
    logger.info(
        "Auto top-up triggered",
        extra={
            "provider_id": row.id,
            "balance": balance,
            "threshold": threshold,
            "topup_amount": amount,
            "mint_url": mint_url,
        },
    )

    try:
        token = await send_token(amount, "sat", mint_url)
    except Exception as e:
        logger.error(
            "Failed to create cashu token for auto top-up",
            extra={
                "provider_id": row.id,
                "amount": amount,
                "mint_url": mint_url,
                "error": str(e),
            },
        )
        return

    try:
        await store_cashu_transaction(
            token=token,
            amount=amount,
            unit="sat",
            mint_url=mint_url,
            typ="out",
            collected=False,
            source="auto_topup",
        )
    except Exception:
        logger.critical(
            "Aborting auto top-up because its cashu token could not be persisted",
            extra={"provider_id": row.id, "mint_url": mint_url},
        )
        return

    result = await provider.topup(token)

    if "error" in result:
        logger.error(
            "Auto top-up upstream call failed",
            extra={
                "provider_id": row.id,
                "error": result["error"],
            },
        )
    else:
        async with create_session() as session:
            transaction = (
                await session.exec(
                    select(CashuTransaction).where(
                        CashuTransaction.token == token,
                        CashuTransaction.type == "out",
                        CashuTransaction.source == "auto_topup",
                    )
                )
            ).first()
            if transaction is None:
                logger.critical(
                    "Completed auto top-up transaction is missing from the database",
                    extra={"provider_id": row.id, "mint_url": mint_url},
                )
            else:
                transaction.collected = True
                session.add(transaction)
                await session.commit()

        logger.info(
            "Auto top-up completed successfully",
            extra={
                "provider_id": row.id,
                "amount": amount,
                "new_balance_approx": balance + amount,
            },
        )


def _ppq_state_id(row: UpstreamProviderRow) -> str:
    if row.id is None:
        raise ValueError("PPQ auto top-up requires a persisted provider row")
    return _ppq_state_id_for_provider(row.id)


def _ppq_state_id_for_provider(provider_id: int | str) -> str:
    return f"ppq-auto-topup-{provider_id}"


def _ppq_payment_id(operation_id: str) -> str:
    return f"ppq-payment-{operation_id}"


class PPQClaim(typing.NamedTuple):
    operation_id: str
    # Worker lease, not the BOLT11 invoice expiry: the invoice can expire
    # while melt() is still running, and only the lease says whether the
    # owning worker can still be alive.
    lease_expires_at: int
    phase: str
    invoice_id: str
    # Cashu melt quote id, "none" until a payment plan exists. This is what
    # lets an ambiguous payment be reconciled against the mint later.
    quote_id: str


def _ppq_request_id(
    operation_id: str,
    lease_expires_at: int,
    phase: str,
    invoice_id: str,
    quote_id: str = "none",
) -> str:
    return f"ppq:{operation_id}:{lease_expires_at}:{phase}:{invoice_id}:{quote_id}"


def _parse_ppq_request_id(request_id: str | None) -> PPQClaim | None:
    parts = (request_id or "").split(":", 5)
    if len(parts) != 6 or parts[0] != "ppq" or parts[3] not in PPQ_PHASES:
        return None
    try:
        lease_expires_at = int(parts[2])
    except (TypeError, ValueError):
        return None
    return PPQClaim(parts[1], lease_expires_at, parts[3], parts[4], parts[5])


def _ppq_claim_is_releasable(claim: PPQClaim | None, now: float) -> bool:
    """Whether an admin may sweep this claim without risking a double payment.

    A claim in ``in_flight`` is owned by a worker that is somewhere between
    reserving proofs and hearing back from the mint. Releasing it there frees
    the next cycle to pay a second invoice, which is exactly what the claim
    exists to prevent — so it is releasable only once its lease has passed,
    which means the owning worker died rather than that it is still working.
    """
    if claim is None:
        # Corrupt state cannot be reasoned about and has no operation id to
        # fence on. Leaving it unreleasable would strand the provider forever.
        return True
    if claim.phase == PPQ_PHASE_IN_FLIGHT:
        return now >= claim.lease_expires_at
    return True


async def get_ppq_auto_topup_state(provider_id: int) -> dict[str, object]:
    """Return admin-safe state for a provider's durable PPQ claim."""
    async with create_session() as session:
        transaction = await session.get(
            CashuTransaction, _ppq_state_id_for_provider(provider_id)
        )
    if transaction is None or transaction.collected or transaction.swept:
        return {"active": False}

    claim = _parse_ppq_request_id(transaction.request_id)
    return {
        "active": True,
        # The exact version of the claim the admin is looking at. A release
        # must echo it back verbatim: the operation id alone is stable across
        # phase changes, so it cannot distinguish "the state I reviewed" from
        # "the same attempt after its payment turned ambiguous".
        "state_token": transaction.request_id,
        "operation_id": claim.operation_id if claim else None,
        "phase": claim.phase if claim else None,
        "releasable": _ppq_claim_is_releasable(claim, time.time()),
        "expires_at": claim.lease_expires_at if claim else None,
        "invoice_id": (
            claim.invoice_id if claim and claim.invoice_id != "pending" else None
        ),
        "created_at": transaction.created_at,
        "amount": transaction.amount,
        "unit": transaction.unit,
        "mint_url": transaction.mint_url,
        "malformed": claim is None,
    }


class PPQReleaseOutcome(typing.NamedTuple):
    released: bool
    reason: str


async def release_ppq_auto_topup_state(
    provider_id: int, *, state_token: str | None
) -> PPQReleaseOutcome:
    """Force-release an active claim after an admin reconciles payment status.

    ``state_token`` is the ``state_token`` the caller read from
    :func:`get_ppq_auto_topup_state` — the claim's full ``request_id``. Two
    things are enforced with it. The claim must not be inside an unexpired
    ``in_flight`` lease, because a worker between reserving proofs and hearing
    from the mint still owns the outcome. And the row must be byte-identical
    to the one the caller reviewed: the update fences on the whole token, so
    any phase change, lease renewal, or new attempt since the review fails the
    write instead of sweeping a state the admin never saw.
    """
    state_id = _ppq_state_id_for_provider(provider_id)
    async with create_session() as session:
        transaction = await session.get(CashuTransaction, state_id)

    if transaction is None or transaction.collected or transaction.swept:
        return PPQReleaseOutcome(False, "no_active_claim")

    if transaction.request_id != state_token:
        return PPQReleaseOutcome(False, "stale_state")

    claim = _parse_ppq_request_id(transaction.request_id)
    if not _ppq_claim_is_releasable(claim, time.time()):
        return PPQReleaseOutcome(False, "payment_in_flight")

    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(
                col(CashuTransaction.id) == state_id,
                col(CashuTransaction.source).in_(
                    ["ppq_auto_topup", "ppq_auto_topup_claim"]
                ),
                # Fence on the token itself, not the row read above: a change
                # between the read and this write must lose the race.
                col(CashuTransaction.request_id) == state_token,
                col(CashuTransaction.collected) == False,  # noqa: E712
                col(CashuTransaction.swept) == False,  # noqa: E712
            )
            .values(swept=True)
        )
        if (getattr(result, "rowcount", 0) or 0) == 1:
            claim = _parse_ppq_request_id(state_token)
            if claim is not None:
                await session.exec(  # type: ignore[call-overload]
                    update(CashuTransaction)
                    .where(
                        col(CashuTransaction.id) == _ppq_payment_id(claim.operation_id)
                    )
                    .values(swept=True)
                )
            await session.commit()
            return PPQReleaseOutcome(True, "released")
        await session.rollback()
        return PPQReleaseOutcome(False, "claim_changed")


async def _set_ppq_state_terminal(
    row: UpstreamProviderRow,
    operation_id: str,
    *,
    collected: bool,
    swept: bool,
) -> bool:
    """Finish a PPQ attempt only if this worker still owns the claim."""
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(
                col(CashuTransaction.id) == _ppq_state_id(row),
                col(CashuTransaction.request_id).like(f"ppq:{operation_id}:%"),
                col(CashuTransaction.collected) == False,  # noqa: E712
                col(CashuTransaction.swept) == False,  # noqa: E712
            )
            .values(collected=collected, swept=swept)
        )
        updated = (getattr(result, "rowcount", 0) or 0) == 1
        if updated:
            await session.exec(  # type: ignore[call-overload]
                update(CashuTransaction)
                .where(col(CashuTransaction.id) == _ppq_payment_id(operation_id))
                .values(collected=collected, swept=swept)
            )
            await session.commit()
        else:
            await session.rollback()
        return updated


async def _reconcile_ppq_state(
    row: UpstreamProviderRow, provider: PPQAIUpstreamProvider | None
) -> bool:
    """Return True while a prior PPQ attempt must suppress a new payment.

    ``provider`` may be ``None`` when the API key is gone: PPQ settlement
    cannot be polled then, but mint-side reconciliation still runs.
    """
    async with create_session() as session:
        transaction = await session.get(CashuTransaction, _ppq_state_id(row))
    if transaction is None or transaction.collected or transaction.swept:
        return False

    claim = _parse_ppq_request_id(transaction.request_id)
    if claim is None:
        logger.critical(
            "Malformed PPQ auto top-up state; suppressing duplicate payment",
            extra={"provider_id": row.id},
        )
        return True

    if claim.invoice_id != "pending":
        if provider is not None and await provider.check_topup_status(claim.invoice_id):
            if not await _set_ppq_state_terminal(
                row, claim.operation_id, collected=True, swept=False
            ):
                # An admin release won the race against a settlement that
                # turned out to have succeeded. The next cycle may pay again;
                # the balance check is the only remaining guard, so shout.
                logger.critical(
                    "PPQ invoice settled but its claim was already released; "
                    "a duplicate top-up is possible on the next cycle",
                    extra={
                        "provider_id": row.id,
                        "invoice_id": claim.invoice_id,
                    },
                )
            return True
        # PPQ has not credited the invoice. Ask the mint what became of the
        # melt — the durable reconciliation path for a payment whose worker
        # died or whose melt call never returned. cashu settles the wallet
        # database as a side effect: "unpaid" releases the reserved proofs.
        if (
            claim.quote_id != "none"
            and transaction.mint_url
            and time.time() >= claim.lease_expires_at
        ):
            status = await check_bolt11_payment_status(
                transaction.mint_url, transaction.unit, claim.quote_id
            )
            if status == "unpaid":
                # Provably never paid, funds recovered — safe to retry.
                released = await _set_ppq_state_terminal(
                    row, claim.operation_id, collected=False, swept=True
                )
                if released:
                    logger.warning(
                        "PPQ auto top-up melt was never paid; claim released",
                        extra={
                            "provider_id": row.id,
                            "invoice_id": claim.invoice_id,
                        },
                    )
                return not released
            # "paid" means the mint paid but PPQ has not credited yet: keep
            # waiting on PPQ. "pending"/"unknown" stay locked for the admin.
        return True
    if time.time() < claim.lease_expires_at:
        return True

    released = await _set_ppq_state_terminal(
        row, claim.operation_id, collected=False, swept=True
    )
    return not released


async def _ppq_provider_is_claimable(
    session: AsyncSession, provider_id: int | None
) -> bool:
    """Re-read the provider inside the claim transaction.

    SQLite serialises write transactions, so checking here — rather than
    trusting the row the cycle loaded earlier — means a concurrent provider
    deletion or type change either commits before us (we see it and refuse)
    or after us (its own claim check sees our claim and refuses). Without
    this the worker could create a claim for a provider that no longer
    exists, orphaning it forever.
    """
    if provider_id is None:
        return False
    current = await session.get(UpstreamProviderRow, provider_id)
    return current is not None and current.provider_type == "ppqai"


async def _claim_ppq_topup(row: UpstreamProviderRow) -> str | None:
    """Acquire a durable, ownership-fenced per-provider claim."""
    state_id = _ppq_state_id(row)
    operation_id = uuid.uuid4().hex
    expires_at = int(time.time()) + PPQ_PENDING_TTL_SECONDS
    request_id = _ppq_request_id(operation_id, expires_at, PPQ_PHASE_CLAIMED, "pending")

    async with create_session() as session:
        if not await _ppq_provider_is_claimable(session, row.id):
            return None
        existing = await session.get(CashuTransaction, state_id)
        if existing is not None:
            result = await session.exec(  # type: ignore[call-overload]
                update(CashuTransaction)
                .where(
                    col(CashuTransaction.id) == state_id,
                    or_(
                        CashuTransaction.collected == True,  # noqa: E712
                        CashuTransaction.swept == True,  # noqa: E712
                    ),
                )
                .values(
                    token="pending",
                    amount=0,
                    unit="sat",
                    mint_url=None,
                    request_id=request_id,
                    collected=False,
                    swept=False,
                    created_at=int(time.time()),
                    source="ppq_auto_topup_claim",
                )
            )
            await session.commit()
            if (getattr(result, "rowcount", 0) or 0) != 1:
                return None
            return operation_id

    try:
        async with create_session() as session:
            # Same fencing as the update path: the provider must still exist
            # inside the transaction that creates the claim.
            if not await _ppq_provider_is_claimable(session, row.id):
                return None
            session.add(
                CashuTransaction(
                    id=state_id,
                    token="pending",
                    amount=0,
                    unit="sat",
                    type="out",
                    request_id=request_id,
                    collected=False,
                    source="ppq_auto_topup_claim",
                )
            )
            await session.commit()
    except IntegrityError:
        return None
    return operation_id


async def _record_ppq_invoice(
    row: UpstreamProviderRow,
    operation_id: str,
    *,
    invoice: str,
    invoice_id: str,
    quote_id: str,
    amount: int,
    unit: str,
    mint_url: str,
) -> int:
    """Move the claim to in_flight and return its fresh worker lease.

    The lease is minted here, at the start of the payment, and deliberately
    not derived from the BOLT11 invoice's expiry: the invoice can expire
    while melt() is still running, and the lease answers a different question
    — can the worker that owns this claim still be alive?
    """
    lease_expires_at = int(time.time()) + PPQ_PENDING_TTL_SECONDS
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(
                col(CashuTransaction.id) == _ppq_state_id(row),
                col(CashuTransaction.request_id).like(
                    f"ppq:{operation_id}:%:{PPQ_PHASE_CLAIMED}:pending:none"
                ),
                col(CashuTransaction.collected) == False,  # noqa: E712
                col(CashuTransaction.swept) == False,  # noqa: E712
            )
            .values(
                token=invoice,
                # Moves the claim to in_flight: from here the proofs are
                # committed and an admin may not release it until the lease
                # runs out.
                request_id=_ppq_request_id(
                    operation_id,
                    lease_expires_at,
                    PPQ_PHASE_IN_FLIGHT,
                    invoice_id,
                    quote_id,
                ),
                amount=amount,
                unit=unit,
                mint_url=mint_url,
            )
        )
        if (getattr(result, "rowcount", 0) or 0) != 1:
            await session.rollback()
            raise RuntimeError("PPQ auto top-up claim ownership was lost")
        session.add(
            CashuTransaction(
                id=_ppq_payment_id(operation_id),
                # Do not expose the raw BOLT11 through the transaction API.
                token=f"ppq-invoice:{invoice_id}",
                amount=amount,
                unit=unit,
                type="out",
                request_id=invoice_id,
                mint_url=mint_url,
                collected=False,
                swept=False,
                source="ppq_auto_topup",
            )
        )
        await session.commit()
    return lease_expires_at


async def _record_ppq_payment_spent(operation_id: str, paid_amount: int) -> None:
    """Persist the irreversible wallet spend before polling PPQ settlement."""
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(col(CashuTransaction.id) == _ppq_payment_id(operation_id))
            .values(amount=paid_amount)
        )
        await session.commit()
        if (getattr(result, "rowcount", 0) or 0) != 1:
            raise RuntimeError("PPQ payment audit row is missing")


async def _mark_ppq_reconcile(
    row: UpstreamProviderRow,
    operation_id: str,
    lease_expires_at: int,
    invoice_id: str,
    quote_id: str,
) -> None:
    """Move an in_flight claim to reconcile so an admin may release it.

    Without this an ambiguous payment stays in_flight, and in_flight is only
    releasable once its lease expires — the admin would have to wait out the
    lease before they could act on an alert that already fired.
    """
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(
                col(CashuTransaction.id) == _ppq_state_id(row),
                col(CashuTransaction.request_id)
                == _ppq_request_id(
                    operation_id,
                    lease_expires_at,
                    PPQ_PHASE_IN_FLIGHT,
                    invoice_id,
                    quote_id,
                ),
                col(CashuTransaction.collected) == False,  # noqa: E712
                col(CashuTransaction.swept) == False,  # noqa: E712
            )
            .values(
                request_id=_ppq_request_id(
                    operation_id,
                    lease_expires_at,
                    PPQ_PHASE_RECONCILE,
                    invoice_id,
                    quote_id,
                )
            )
        )
        await session.commit()
        if (getattr(result, "rowcount", 0) or 0) != 1:
            logger.warning(
                "Could not flag the PPQ claim for reconciliation; "
                "it is no longer owned by this attempt",
                extra={"provider_id": row.id, "invoice_id": invoice_id},
            )


async def _check_and_topup_ppq(row: UpstreamProviderRow, settings: dict) -> None:
    threshold_usd = float(settings["topup_threshold"])
    amount_usd = int(settings["topup_amount_limit"])
    provider = PPQAIUpstreamProvider.from_db_row(row)
    if provider is None or await _reconcile_ppq_state(row, provider):
        return

    balance = await provider.get_balance()
    if balance is None or not math.isfinite(balance) or balance < 0:
        logger.warning(
            "Could not fetch a valid PPQ balance for auto top-up",
            extra={"provider_id": row.id},
        )
        return
    if balance >= threshold_usd:
        return

    # Perform local pricing and owner-funds checks before asking PPQ to create
    # an invoice. The exact mint quote still has to be checked afterward, but
    # predictable local failures should not leave abandoned PPQ invoices.
    price = sats_usd_price()
    minimum_invoice_sats = math.ceil(amount_usd / price)
    max_invoice_sats = math.ceil(minimum_invoice_sats * PPQ_MAX_INVOICE_PREMIUM)
    if await maximum_owner_cashu_balance_sats() < minimum_invoice_sats:
        logger.warning(
            "PPQ auto top-up skipped because no mint has enough owner funds",
            extra={
                "provider_id": row.id,
                "minimum_invoice_sats": minimum_invoice_sats,
            },
        )
        return

    operation_id = await _claim_ppq_topup(row)
    if operation_id is None:
        return

    logger.info(
        "PPQ auto top-up triggered",
        extra={
            "provider_id": row.id,
            "balance_usd": balance,
            "threshold_usd": threshold_usd,
            "topup_usd": amount_usd,
        },
    )

    try:
        topup = await provider.initiate_topup(amount_usd)
        if topup.currency.upper() != "USD" or topup.amount != amount_usd:
            raise ValueError("PPQ top-up response amount or currency does not match")

        now = int(time.time())
        # The invoice's own expiry is a pre-payment sanity check only; the
        # claim's lease is minted separately in _record_ppq_invoice.
        invoice_expires_at = topup.expires_at or now + PPQ_PENDING_TTL_SECONDS
        if invoice_expires_at > 10**12:
            invoice_expires_at //= 1000
        if invoice_expires_at <= now:
            raise ValueError("PPQ returned an expired Lightning invoice")

        plan = await prepare_bolt11_payment(topup.payment_request)
        if plan.maximum_spend_sats > max_invoice_sats:
            raise ValueError("PPQ Lightning invoice exceeds the USD spending cap")

        lease_expires_at = await _record_ppq_invoice(
            row,
            operation_id,
            invoice=topup.payment_request,
            invoice_id=topup.invoice_id,
            quote_id=str(plan.quote.quote),
            amount=int(plan.quote.amount + plan.quote.fee_reserve),
            unit=plan.unit,
            mint_url=plan.mint_url,
        )
    except Exception:
        # Nothing has been paid yet, so the claim can be handed back. If the
        # release does not land, the claim is no longer ours to reason about.
        if not await _set_ppq_state_terminal(
            row, operation_id, collected=False, swept=True
        ):
            logger.warning(
                "Could not release the PPQ auto top-up claim after a "
                "pre-payment failure; it is owned by another attempt",
                extra={"provider_id": row.id},
            )
        raise

    try:
        paid_amount, mint_url, unit = await execute_bolt11_payment(plan)
    except Bolt11PaymentNotAttempted:
        # The mint's own answer rules out a settlement and any reserved proofs
        # were handed back, so this claim is safe to retry next cycle.
        if not await _set_ppq_state_terminal(
            row, operation_id, collected=False, swept=True
        ):
            logger.warning(
                "Could not release the PPQ auto top-up claim after a payment "
                "that was never attempted; it is owned by another attempt",
                extra={"provider_id": row.id},
            )
        logger.warning(
            "PPQ Lightning payment was not attempted; claim released for retry",
            extra={"provider_id": row.id, "invoice_id": topup.invoice_id},
            exc_info=True,
        )
        raise
    except Bolt11PaymentAmbiguous:
        await _mark_ppq_reconcile(
            row,
            operation_id,
            lease_expires_at,
            topup.invoice_id,
            str(plan.quote.quote),
        )
        logger.critical(
            "PPQ auto top-up payment outcome is ambiguous; claim remains locked until admin reconciliation",
            extra={
                "provider_id": row.id,
                "invoice_id": topup.invoice_id,
                "admin_action": f"POST /admin/api/upstream-providers/{row.id}/ppq-auto-topup/release",
            },
            exc_info=True,
        )
        raise
    except BaseException:
        # Cancellation or an unexpected error after execution began is also
        # ambiguous. Preserve the claim before propagating it.
        await asyncio.shield(
            _mark_ppq_reconcile(
                row,
                operation_id,
                lease_expires_at,
                topup.invoice_id,
                str(plan.quote.quote),
            )
        )
        logger.critical(
            "Unexpected failure while paying PPQ invoice; payment requires reconciliation",
            extra={"provider_id": row.id, "invoice_id": topup.invoice_id},
            exc_info=True,
        )
        raise

    try:
        # The melt is irreversible now. Persist the actual spend before any
        # fallible PPQ status call so operators retain an audit trail.
        await _record_ppq_payment_spent(operation_id, paid_amount)
        settled = False
        for attempt in range(PPQ_SETTLEMENT_ATTEMPTS):
            if await provider.check_topup_status(topup.invoice_id):
                settled = True
                break
            if attempt + 1 < PPQ_SETTLEMENT_ATTEMPTS:
                await asyncio.sleep(PPQ_SETTLEMENT_POLL_SECONDS)
    except BaseException as error:
        await asyncio.shield(
            _mark_ppq_reconcile(
                row,
                operation_id,
                lease_expires_at,
                topup.invoice_id,
                str(plan.quote.quote),
            )
        )
        logger.critical(
            "PPQ Lightning payment completed but settlement polling failed; reconciliation required",
            extra={
                "provider_id": row.id,
                "invoice_id": topup.invoice_id,
                "error": str(error),
                "error_type": type(error).__name__,
            },
            exc_info=True,
        )
        if isinstance(error, asyncio.CancelledError):
            raise
        return

    if not settled:
        # The payment left the wallet, so the claim must stay. Flag it for
        # reconciliation: _reconcile_ppq_state keeps polling PPQ, and an admin
        # can step in without waiting out the lease.
        await _mark_ppq_reconcile(
            row,
            operation_id,
            lease_expires_at,
            topup.invoice_id,
            str(plan.quote.quote),
        )
        logger.critical(
            "PPQ Lightning payment completed but credit settlement is unconfirmed",
            extra={"provider_id": row.id, "invoice_id": topup.invoice_id},
        )
        return

    if not await _set_ppq_state_terminal(
        row, operation_id, collected=True, swept=False
    ):
        # Something else finished this claim while the payment was in flight —
        # an admin release, most likely. The next cycle is now free to pay
        # again, so surface it rather than completing quietly.
        logger.critical(
            "PPQ auto top-up settled but its claim was already released; "
            "a duplicate top-up is possible on the next cycle",
            extra={"provider_id": row.id, "invoice_id": topup.invoice_id},
        )
    logger.info(
        "PPQ auto top-up completed",
        extra={
            "provider_id": row.id,
            "topup_usd": amount_usd,
            "cashu_paid": paid_amount,
            "cashu_unit": unit,
            "mint_url": mint_url,
        },
    )
