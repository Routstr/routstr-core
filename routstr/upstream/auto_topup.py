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
from ..payment.price import sats_usd_price
from ..wallet import (
    Bolt11PaymentAmbiguous,
    Bolt11PaymentNotAttempted,
    check_bolt11_payment_status,
    execute_bolt11_payment,
    maximum_owner_cashu_balance_sats,
    prepare_bolt11_payment,
    release_token_reservation,
    send_token_from_owner_locked,
    token_mint_url,
    wallet_operation_guard,
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
PPQ_SETTLED_COOLDOWN_SECONDS = 5 * 60
PPQ_MAX_INVOICE_PREMIUM = 1.10
PPQ_MIN_TOPUP_USD = 1
PPQ_MAX_TOPUP_USD = 500
# Rolling 24h ceiling on total PPQ auto top-up spend across all providers.
# Independent of PPQ's own balance endpoint: if that endpoint is buggy or
# compromised and keeps reporting a below-threshold balance, this cap bounds
# the damage instead of letting the worker drain the owner's mint funds one
# per-transaction-capped payment at a time.
PPQ_MAX_DAILY_TOPUP_USD = 300
# Routstr-to-Routstr claim lifecycle. "claimed" holds the slot while the token
# is being minted; nothing has left the wallet yet. "sent" means a bearer token
# was handed to the peer and only the peer's balance can say whether it landed.
# "backoff" holds the failure count between attempts, and "halted" stops the
# provider entirely until an admin releases it.
ROUTSTR_PHASE_CLAIMED = "claimed"
ROUTSTR_PHASE_SENT = "sent"
ROUTSTR_PHASE_BACKOFF = "backoff"
ROUTSTR_PHASE_HALTED = "halted"
ROUTSTR_PHASES = frozenset(
    {
        ROUTSTR_PHASE_CLAIMED,
        ROUTSTR_PHASE_SENT,
        ROUTSTR_PHASE_BACKOFF,
        ROUTSTR_PHASE_HALTED,
    }
)
ROUTSTR_PENDING_TTL_SECONDS = 15 * 60
ROUTSTR_BACKOFF_BASE_SECONDS = 15 * 60
ROUTSTR_MAX_TOPUP_FAILURES = 3
ROUTSTR_MIN_TOPUP_SATS = 1
ROUTSTR_MAX_TOPUP_SATS = 1_000_000
# Rolling 24h ceiling on total Routstr auto top-up spend across all peers. The
# per-attempt claim already stops a peer from being paid twice for the same
# uncredited token; this bounds the total even when every attempt is credited
# and the peer simply keeps reporting a below-threshold balance.
ROUTSTR_MAX_DAILY_TOPUP_SATS = 2_000_000


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
                extra={"error": repr(e), "error_type": type(e).__name__},
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
                    "error": repr(e),
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
                extra={
                    "provider_id": row.id,
                    "error": repr(e),
                    "error_type": type(e).__name__,
                },
            )
    return active_provider_ids


def _invalid_topup_number(value: object, *, integer: bool = False) -> bool:
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
    if _invalid_topup_number(threshold):
        return "PPQ auto top-up threshold must be a positive number"
    if _invalid_topup_number(amount, integer=True):
        return "PPQ auto top-up amount must be a positive whole number"
    amount_usd = int(typing.cast(int | float, amount))
    if not PPQ_MIN_TOPUP_USD <= amount_usd <= PPQ_MAX_TOPUP_USD:
        return (
            f"PPQ auto top-up amount must be between {PPQ_MIN_TOPUP_USD} "
            f"and {PPQ_MAX_TOPUP_USD} USD"
        )
    return None


_legacy_threshold_hinted: set[int] = set()


def _routstr_threshold_sats(row: UpstreamProviderRow, settings: dict) -> float:
    """Balance, in sats, below which a top-up fires.

    ``topup_threshold_sats`` is used as written. A legacy ``topup_threshold``
    keeps the thousandfold it has always been compared with — reinterpreting it
    as sats would drop an operator's trigger point by a factor of 1000 on
    upgrade and leave the peer to run dry. The hint names the value to migrate
    to, once per provider rather than once per scheduler tick.
    """
    explicit = settings.get("topup_threshold_sats")
    if explicit is not None:
        return float(typing.cast(int | float, explicit))

    threshold_sats = float(typing.cast(int | float, settings["topup_threshold"])) * 1000
    if row.id is not None and row.id not in _legacy_threshold_hinted:
        _legacy_threshold_hinted.add(row.id)
        logger.warning(
            "Routstr auto top-up uses the legacy unitless threshold; set "
            "topup_threshold_sats to state the unit",
            extra={"provider_id": row.id, "threshold_sats": threshold_sats},
        )
    return threshold_sats


def validate_routstr_auto_topup_settings(settings: dict | None) -> str | None:
    """Return why enabled Routstr auto top-up settings are invalid, if anything."""
    if not settings or not settings.get("auto_topup"):
        return None

    threshold_sats = settings.get("topup_threshold_sats")
    legacy_threshold = settings.get("topup_threshold")
    amount = settings.get("topup_amount_limit")
    mint_url = settings.get("topup_mint_url")
    if threshold_sats is not None:
        if _invalid_topup_number(threshold_sats):
            return "Routstr auto top-up threshold must be a positive number of sats"
    elif legacy_threshold is None:
        return "Routstr auto top-up requires a threshold"
    elif _invalid_topup_number(legacy_threshold):
        return "Routstr auto top-up threshold must be a positive number"
    if _invalid_topup_number(amount, integer=True):
        return "Routstr auto top-up amount must be a positive whole number"
    amount_sats = int(typing.cast(int | float, amount))
    if not ROUTSTR_MIN_TOPUP_SATS <= amount_sats <= ROUTSTR_MAX_TOPUP_SATS:
        return (
            f"Routstr auto top-up amount must be between {ROUTSTR_MIN_TOPUP_SATS} "
            f"and {ROUTSTR_MAX_TOPUP_SATS} sats"
        )
    if not isinstance(mint_url, str) or not mint_url.strip():
        return "Routstr auto top-up requires a mint URL"
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

    problem = validate_routstr_auto_topup_settings(settings)
    if problem is not None:
        logger.warning(
            "Auto top-up enabled but its configuration is invalid",
            extra={"provider_id": row.id, "problem": problem},
        )
        return

    threshold_sats = _routstr_threshold_sats(row, settings)
    amount = int(settings["topup_amount_limit"])
    mint_url = str(settings["topup_mint_url"])

    if not row.api_key:
        return

    # Instantiate provider and check balance
    provider = RoutstrUpstreamProvider.from_db_row(row)
    if provider is None:
        return
    if await _reconcile_routstr_state(row, provider):
        return

    balance = await provider.get_balance()

    if balance is None or not math.isfinite(balance) or balance < 0:
        logger.warning(
            "Could not fetch balance for auto top-up",
            extra={"provider_id": row.id, "base_url": row.base_url},
        )
        return

    if balance >= threshold_sats:
        return

    spent_24h_sats = await _routstr_spent_last_24h_sats()
    if spent_24h_sats + amount > ROUTSTR_MAX_DAILY_TOPUP_SATS:
        logger.critical(
            "Auto top-up skipped: rolling 24h spend cap reached",
            extra={
                "provider_id": row.id,
                "spent_24h_sats": spent_24h_sats,
                "topup_amount": amount,
                "daily_cap_sats": ROUTSTR_MAX_DAILY_TOPUP_SATS,
            },
        )
        return

    # The balance the peer must report before another token may be sent. Any
    # shortfall is treated as "not credited": the token is a bearer instrument
    # and a peer that took one without crediting it must not be handed another.
    expected_sats = math.floor(balance) + amount
    operation_id = await _claim_routstr_topup(row, expected_sats=expected_sats)
    if operation_id is None:
        return

    # Balance is below threshold - create token and top up
    logger.info(
        "Auto top-up triggered",
        extra={
            "provider_id": row.id,
            "balance": balance,
            "threshold_sats": threshold_sats,
            "topup_amount": amount,
            "mint_url": mint_url,
        },
    )

    try:
        async with wallet_operation_guard():
            # The cap, owner-liability check, proof reservation, and outgoing
            # audit row share one wallet mutation scope. The audit row must be
            # durable before another worker can recheck the rolling cap.
            spent_24h_sats = await _routstr_spent_last_24h_sats()
            if spent_24h_sats + amount > ROUTSTR_MAX_DAILY_TOPUP_SATS:
                raise ValueError("Routstr auto top-up daily spend cap reached")
            token = await send_token_from_owner_locked(amount, "sat", mint_url)
            actual_mint_url = token_mint_url(token, mint_url)
            try:
                await _persist_routstr_token_and_mark_sent(
                    row,
                    operation_id,
                    expected_sats=expected_sats,
                    token=token,
                    amount=amount,
                    mint_url=actual_mint_url,
                )
            except Exception:
                logger.critical(
                    "Aborting auto top-up because its token and sent claim "
                    "could not be persisted atomically",
                    extra={"provider_id": row.id, "mint_url": actual_mint_url},
                )
                try:
                    await release_token_reservation(token)
                except Exception as error:
                    logger.critical(
                        "Failed to release untracked auto-topup token",
                        extra={
                            "provider_id": row.id,
                            "mint_url": actual_mint_url,
                            "error": repr(error),
                        },
                    )
                else:
                    logger.warning(
                        "Auto-topup token was released after persistence failed",
                        extra={"provider_id": row.id, "mint_url": actual_mint_url},
                    )
                raise
    except Exception as e:
        logger.warning(
            "Failed to create or persist cashu token for auto top-up",
            extra={
                "provider_id": row.id,
                "amount": amount,
                "mint_url": mint_url,
                "error": repr(e),
                "error_type": type(e).__name__,
            },
        )
        await _release_routstr_claim(row, operation_id)
        return

    # The audit row and SENT claim committed together before this network call,
    # so a worker crash cannot make reconciliation treat reserved proofs as an
    # unspent CLAIMED attempt.
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


def _routstr_state_id(row: UpstreamProviderRow) -> str:
    if row.id is None:
        raise ValueError("Routstr auto top-up requires a persisted provider row")
    return _routstr_state_id_for_provider(row.id)


def _routstr_state_id_for_provider(provider_id: int | str) -> str:
    return f"routstr-auto-topup-{provider_id}"


class RoutstrClaim(typing.NamedTuple):
    operation_id: str
    # Worker lease for "claimed"/"sent", retry-not-before for "backoff", and
    # meaningless for "halted".
    deadline: int
    phase: str
    # Peer balance in sats that proves this attempt was credited.
    expected_sats: int
    failures: int


def _routstr_request_id(
    operation_id: str,
    deadline: int,
    phase: str,
    expected_sats: int,
    failures: int,
) -> str:
    return f"routstr:{operation_id}:{deadline}:{phase}:{expected_sats}:{failures}"


def _parse_routstr_request_id(request_id: str | None) -> RoutstrClaim | None:
    parts = (request_id or "").split(":", 5)
    if len(parts) != 6 or parts[0] != "routstr" or parts[3] not in ROUTSTR_PHASES:
        return None
    try:
        deadline = int(parts[2])
        expected_sats = int(parts[4])
        failures = int(parts[5])
    except (TypeError, ValueError):
        return None
    return RoutstrClaim(parts[1], deadline, parts[3], expected_sats, failures)


async def _routstr_spent_last_24h_sats() -> int:
    """Total sats committed to Routstr auto top-ups in the last 24 hours.

    Uncollected rows count too: a token whose delivery is unconfirmed is spent
    for capping purposes. Rows marked ``collected=False, swept=True`` record
    tokens that were provably returned to the wallet and are excluded.
    """
    cutoff = int(time.time()) - 24 * 60 * 60
    async with create_session() as session:
        rows = (
            await session.exec(
                select(CashuTransaction.amount, CashuTransaction.unit).where(
                    col(CashuTransaction.source) == "auto_topup",
                    col(CashuTransaction.type) == "out",
                    col(CashuTransaction.created_at) >= cutoff,
                    or_(
                        col(CashuTransaction.collected) == True,  # noqa: E712
                        col(CashuTransaction.swept) == False,  # noqa: E712
                    ),
                )
            )
        ).all()
    return sum(
        amount if unit == "sat" else math.ceil(amount / 1000) for amount, unit in rows
    )


async def _routstr_provider_is_claimable(
    session: AsyncSession, provider_id: int | str | None
) -> bool:
    """Re-read the provider inside the claim transaction.

    Same reasoning as :func:`_ppq_provider_is_claimable`: a provider deleted or
    retyped concurrently must either be visible here or lose the race against
    the claim we are about to write.
    """
    if provider_id is None:
        return False
    current = await session.get(UpstreamProviderRow, provider_id)
    return current is not None and current.provider_type == "routstr"


async def _claim_routstr_topup(
    row: UpstreamProviderRow, *, expected_sats: int
) -> str | None:
    """Acquire the provider's single durable auto top-up slot.

    An expired backoff hands its failure count to the new attempt, so repeated
    non-crediting peers still walk towards the halt instead of resetting the
    counter every cycle.
    """
    state_id = _routstr_state_id(row)
    operation_id = uuid.uuid4().hex
    deadline = int(time.time()) + ROUTSTR_PENDING_TTL_SECONDS

    async with create_session() as session:
        if not await _routstr_provider_is_claimable(session, row.id):
            return None
        existing = await session.get(CashuTransaction, state_id)
        if existing is not None:
            failures = 0
            if not (existing.collected or existing.swept):
                claim = _parse_routstr_request_id(existing.request_id)
                if (
                    claim is None
                    or claim.phase != ROUTSTR_PHASE_BACKOFF
                    or time.time() < claim.deadline
                ):
                    return None
                failures = claim.failures
            result = await session.exec(  # type: ignore[call-overload]
                update(CashuTransaction)
                .where(
                    col(CashuTransaction.id) == state_id,
                    # Fence on the exact row that was read: any concurrent
                    # writer that moved the claim must win instead of us.
                    col(CashuTransaction.request_id) == existing.request_id,
                )
                .values(
                    token="pending",
                    amount=0,
                    unit="sat",
                    mint_url=None,
                    request_id=_routstr_request_id(
                        operation_id,
                        deadline,
                        ROUTSTR_PHASE_CLAIMED,
                        expected_sats,
                        failures,
                    ),
                    collected=False,
                    swept=False,
                    created_at=int(time.time()),
                    source="routstr_auto_topup_claim",
                )
            )
            await session.commit()
            if (getattr(result, "rowcount", 0) or 0) != 1:
                return None
            return operation_id

    try:
        async with create_session() as session:
            if not await _routstr_provider_is_claimable(session, row.id):
                return None
            session.add(
                CashuTransaction(
                    id=state_id,
                    token="pending",
                    amount=0,
                    unit="sat",
                    type="out",
                    request_id=_routstr_request_id(
                        operation_id,
                        deadline,
                        ROUTSTR_PHASE_CLAIMED,
                        expected_sats,
                        0,
                    ),
                    collected=False,
                    source="routstr_auto_topup_claim",
                )
            )
            await session.commit()
    except IntegrityError:
        return None
    return operation_id


async def _advance_routstr_claim(
    row: UpstreamProviderRow,
    operation_id: str,
    *,
    deadline: int,
    phase: str,
    expected_sats: int,
    failures: int,
    token: str | None = None,
    amount: int | None = None,
    mint_url: str | None = None,
) -> bool:
    """Move this worker's claim to another phase, if it still owns it."""
    values: dict[str, object] = {
        "request_id": _routstr_request_id(
            operation_id, deadline, phase, expected_sats, failures
        )
    }
    if token is not None:
        values.update(token=token, amount=amount, mint_url=mint_url)

    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(
                col(CashuTransaction.id) == _routstr_state_id(row),
                col(CashuTransaction.request_id).like(f"routstr:{operation_id}:%"),
                col(CashuTransaction.collected) == False,  # noqa: E712
                col(CashuTransaction.swept) == False,  # noqa: E712
            )
            .values(**values)
        )
        await session.commit()
        return (getattr(result, "rowcount", 0) or 0) == 1


async def _set_routstr_state_terminal(
    row: UpstreamProviderRow, operation_id: str, *, collected: bool, swept: bool
) -> bool:
    """Finish an attempt only if this worker still owns the claim."""
    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(
                col(CashuTransaction.id) == _routstr_state_id(row),
                col(CashuTransaction.request_id).like(f"routstr:{operation_id}:%"),
                col(CashuTransaction.collected) == False,  # noqa: E712
                col(CashuTransaction.swept) == False,  # noqa: E712
            )
            .values(collected=collected, swept=swept)
        )
        await session.commit()
        return (getattr(result, "rowcount", 0) or 0) == 1


async def _release_routstr_claim(row: UpstreamProviderRow, operation_id: str) -> None:
    """Hand back a claim whose token never left the wallet."""
    if not await _set_routstr_state_terminal(
        row, operation_id, collected=False, swept=True
    ):
        logger.warning(
            "Could not release the auto top-up claim after a pre-send failure; "
            "it is owned by another attempt",
            extra={"provider_id": row.id},
        )


async def _persist_routstr_token_and_mark_sent(
    row: UpstreamProviderRow,
    operation_id: str,
    *,
    expected_sats: int,
    token: str,
    amount: int,
    mint_url: str,
) -> None:
    """Commit the bearer-token audit row and SENT claim atomically."""
    state_id = _routstr_state_id(row)
    async with create_session() as session:
        state = await session.get(CashuTransaction, state_id)
        claim = _parse_routstr_request_id(state.request_id if state else None)
        if (
            state is None
            or state.collected
            or state.swept
            or claim is None
            or claim.operation_id != operation_id
            or claim.phase != ROUTSTR_PHASE_CLAIMED
        ):
            raise RuntimeError("Routstr auto top-up claim ownership was lost")

        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(
                col(CashuTransaction.id) == state_id,
                col(CashuTransaction.request_id) == state.request_id,
                col(CashuTransaction.collected) == False,  # noqa: E712
                col(CashuTransaction.swept) == False,  # noqa: E712
            )
            .values(
                request_id=_routstr_request_id(
                    operation_id,
                    int(time.time()) + ROUTSTR_PENDING_TTL_SECONDS,
                    ROUTSTR_PHASE_SENT,
                    expected_sats,
                    claim.failures,
                ),
                token=token,
                amount=amount,
                unit="sat",
                mint_url=mint_url,
            )
        )
        if (getattr(result, "rowcount", 0) or 0) != 1:
            await session.rollback()
            raise RuntimeError("Routstr auto top-up claim ownership was lost")

        session.add(
            CashuTransaction(
                id=uuid.uuid4().hex,
                token=token,
                amount=amount,
                unit="sat",
                mint_url=mint_url,
                type="out",
                collected=False,
                source="auto_topup",
            )
        )
        await session.commit()


async def _current_routstr_claim(row: UpstreamProviderRow) -> RoutstrClaim | None:
    async with create_session() as session:
        transaction = await session.get(CashuTransaction, _routstr_state_id(row))
    if transaction is None:
        return None
    return _parse_routstr_request_id(transaction.request_id)


async def _reconcile_routstr_state(
    row: UpstreamProviderRow, provider: RoutstrUpstreamProvider
) -> bool:
    """Return True while a prior attempt must suppress a new payment."""
    async with create_session() as session:
        transaction = await session.get(CashuTransaction, _routstr_state_id(row))
    if transaction is None or transaction.collected or transaction.swept:
        return False

    claim = _parse_routstr_request_id(transaction.request_id)
    if claim is None:
        logger.critical(
            "Malformed auto top-up state; suppressing duplicate payment",
            extra={"provider_id": row.id},
        )
        return True

    now = time.time()
    if claim.phase == ROUTSTR_PHASE_HALTED:
        return True
    if claim.phase == ROUTSTR_PHASE_BACKOFF:
        return now < claim.deadline
    if claim.phase == ROUTSTR_PHASE_CLAIMED:
        # Nothing left the wallet, so a dead worker's slot is free to reuse.
        if now < claim.deadline:
            return True
        return not await _set_routstr_state_terminal(
            row, claim.operation_id, collected=False, swept=True
        )

    balance = await provider.get_balance()
    if (
        balance is not None
        and math.isfinite(balance)
        and balance >= claim.expected_sats
    ):
        if not await _set_routstr_state_terminal(
            row, claim.operation_id, collected=True, swept=False
        ):
            logger.critical(
                "Auto top-up was credited but its claim was already released; "
                "a duplicate top-up is possible on the next cycle",
                extra={"provider_id": row.id},
            )
        return True
    if now < claim.deadline:
        return True

    failures = claim.failures + 1
    if failures >= ROUTSTR_MAX_TOPUP_FAILURES:
        await _advance_routstr_claim(
            row,
            claim.operation_id,
            deadline=claim.deadline,
            phase=ROUTSTR_PHASE_HALTED,
            expected_sats=claim.expected_sats,
            failures=failures,
        )
        logger.critical(
            "Auto top-up halted: the peer repeatedly failed to credit a token",
            extra={
                "provider_id": row.id,
                "base_url": row.base_url,
                "failures": failures,
                "admin_action": (
                    f"POST /admin/api/upstream-providers/{row.id}"
                    "/routstr-auto-topup/release"
                ),
            },
        )
        return True

    await _advance_routstr_claim(
        row,
        claim.operation_id,
        deadline=int(now) + ROUTSTR_BACKOFF_BASE_SECONDS * 2 ** (failures - 1),
        phase=ROUTSTR_PHASE_BACKOFF,
        expected_sats=claim.expected_sats,
        failures=failures,
    )
    logger.warning(
        "Auto top-up was not credited by the peer; backing off",
        extra={
            "provider_id": row.id,
            "base_url": row.base_url,
            "expected_sats": claim.expected_sats,
            "failures": failures,
        },
    )
    return True


async def get_routstr_auto_topup_state(provider_id: int) -> dict[str, object]:
    """Return admin-safe state for a provider's durable Routstr claim."""
    async with create_session() as session:
        transaction = await session.get(
            CashuTransaction, _routstr_state_id_for_provider(provider_id)
        )
    if transaction is None or transaction.collected or transaction.swept:
        return {"active": False}

    claim = _parse_routstr_request_id(transaction.request_id)
    return {
        "active": True,
        # Echoed back verbatim on release so a claim that moved on since the
        # admin reviewed it fails the write instead of being swept unseen.
        "state_token": transaction.request_id,
        "operation_id": claim.operation_id if claim else None,
        "phase": claim.phase if claim else None,
        "expected_sats": claim.expected_sats if claim else None,
        "failures": claim.failures if claim else None,
        "deadline": claim.deadline if claim else None,
        "created_at": transaction.created_at,
        "amount": transaction.amount,
        "unit": transaction.unit,
        "mint_url": transaction.mint_url,
        "malformed": claim is None,
    }


class RoutstrReleaseOutcome(typing.NamedTuple):
    released: bool
    reason: str


async def release_routstr_auto_topup_state(
    provider_id: int, *, state_token: str | None
) -> RoutstrReleaseOutcome:
    """Clear a halted or stuck claim after an admin reconciles the peer.

    Unlike a Lightning melt there is no in-flight window to protect: the token
    is already with the peer or still in the wallet either way. What the fence
    does protect is the admin's decision — the row must be byte-identical to
    the one they reviewed, so a claim that advanced in the meantime is not
    swept on the strength of stale information.
    """
    state_id = _routstr_state_id_for_provider(provider_id)
    async with create_session() as session:
        transaction = await session.get(CashuTransaction, state_id)

    if transaction is None or transaction.collected or transaction.swept:
        return RoutstrReleaseOutcome(False, "no_active_claim")
    if transaction.request_id != state_token:
        return RoutstrReleaseOutcome(False, "stale_state")

    async with create_session() as session:
        result = await session.exec(  # type: ignore[call-overload]
            update(CashuTransaction)
            .where(
                col(CashuTransaction.id) == state_id,
                col(CashuTransaction.request_id) == state_token,
                col(CashuTransaction.collected) == False,  # noqa: E712
                col(CashuTransaction.swept) == False,  # noqa: E712
            )
            .values(swept=True)
        )
        if (getattr(result, "rowcount", 0) or 0) == 1:
            await session.commit()
            return RoutstrReleaseOutcome(True, "released")
        await session.rollback()
        return RoutstrReleaseOutcome(False, "claim_changed")


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
            .values(
                collected=collected,
                swept=swept,
                # For successful payments this timestamps the durable cooldown,
                # not merely when the original claim was created.
                created_at=int(time.time()) if collected else CashuTransaction.created_at,
            )
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
    if transaction is None or transaction.swept:
        return False
    if transaction.collected:
        return int(time.time()) - transaction.created_at < PPQ_SETTLED_COOLDOWN_SECONDS

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
    session: AsyncSession, row: UpstreamProviderRow
) -> bool:
    """Re-read the provider inside the claim transaction.

    SQLite serialises write transactions, so checking here — rather than
    trusting the row the cycle loaded earlier — means a concurrent provider
    deletion or type change either commits before us (we see it and refuse)
    or after us (its own claim check sees our claim and refuses). Without
    this the worker could create a claim for a provider that no longer
    exists, orphaning it forever.
    """
    if row.id is None:
        return False
    current = await session.get(UpstreamProviderRow, row.id)
    return bool(
        current is not None
        and current.enabled
        and current.provider_type == "ppqai"
        and current.api_key == row.api_key
        and current.provider_settings == row.provider_settings
    )


async def _claim_ppq_topup(row: UpstreamProviderRow) -> str | None:
    """Acquire a durable, ownership-fenced per-provider claim."""
    state_id = _ppq_state_id(row)
    operation_id = uuid.uuid4().hex
    expires_at = int(time.time()) + PPQ_PENDING_TTL_SECONDS
    request_id = _ppq_request_id(operation_id, expires_at, PPQ_PHASE_CLAIMED, "pending")

    async with create_session() as session:
        if not await _ppq_provider_is_claimable(session, row):
            return None
        existing = await session.get(CashuTransaction, state_id)
        if existing is not None:
            if (
                existing.collected
                and not existing.swept
                and int(time.time()) - existing.created_at
                < PPQ_SETTLED_COOLDOWN_SECONDS
            ):
                return None
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
            if not await _ppq_provider_is_claimable(session, row):
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
    amount_usd: int,
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
                # The USD amount is stamped here so the daily spend cap can
                # aggregate what each payment was worth when it was made,
                # independent of later BTC price moves.
                token=f"ppq-invoice:{invoice_id}:usd:{amount_usd}",
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


def _ppq_payment_usd(amount: int, unit: str, token: str, price: float) -> float:
    """USD value of one PPQ payment audit row.

    Prefers the USD amount stamped into the token when the payment was
    recorded: converting stored sats at today's price would undercount past
    spend whenever the BTC price has fallen since. Falls back to a current
    price conversion for rows recorded before the stamp existed.
    """
    marker = ":usd:"
    if marker in token:
        try:
            return float(token.rsplit(marker, 1)[1])
        except ValueError:
            pass
    sats = amount if unit == "sat" else math.ceil(amount / 1000)
    return sats * price


async def _ppq_spent_last_24h_usd(price: float) -> float:
    """Total USD committed to PPQ top-ups in the last 24 hours.

    Counts in-flight and ambiguous payments — for spend-cap purposes an
    unresolved payment must be assumed spent — but not rows marked
    ``collected=False, swept=True``, which record payments the mint provably
    never attempted; those must not starve future top-ups for a day.
    """
    cutoff = int(time.time()) - 24 * 60 * 60
    async with create_session() as session:
        rows = (
            await session.exec(
                select(
                    CashuTransaction.amount,
                    CashuTransaction.unit,
                    CashuTransaction.token,
                ).where(
                    col(CashuTransaction.source) == "ppq_auto_topup",
                    col(CashuTransaction.type) == "out",
                    col(CashuTransaction.created_at) >= cutoff,
                    or_(
                        col(CashuTransaction.collected) == True,  # noqa: E712
                        col(CashuTransaction.swept) == False,  # noqa: E712
                    ),
                )
            )
        ).all()
    return sum(
        _ppq_payment_usd(amount, unit, token, price) for amount, unit, token in rows
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

    # A single stale/partial balance response must never create an invoice.
    # Read the uncached endpoint again and require independent agreement.
    confirmed_balance = await provider.get_balance()
    if (
        confirmed_balance is None
        or not math.isfinite(confirmed_balance)
        or confirmed_balance < 0
        or confirmed_balance >= threshold_usd
    ):
        logger.info(
            "PPQ auto top-up aborted by balance confirmation",
            extra={
                "provider_id": row.id,
                "first_balance_usd": balance,
                "confirmed_balance_usd": confirmed_balance,
                "threshold_usd": threshold_usd,
            },
        )
        return
    balance = confirmed_balance

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

    # Cheap early check to avoid claim and invoice churn; the authoritative
    # re-check happens under the wallet guard just before payment, where no
    # concurrent worker can move the total.
    spent_24h_usd = await _ppq_spent_last_24h_usd(price)
    if spent_24h_usd + amount_usd > PPQ_MAX_DAILY_TOPUP_USD:
        logger.critical(
            "PPQ auto top-up skipped: rolling 24h spend cap reached",
            extra={
                "provider_id": row.id,
                "spent_24h_usd": round(spent_24h_usd, 2),
                "topup_usd": amount_usd,
                "daily_cap_usd": PPQ_MAX_DAILY_TOPUP_USD,
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

    # Hold the wallet guard across planning and execution: the plan snapshots
    # live proof state, and another worker process reserving or spending those
    # proofs between the two calls would invalidate the snapshot.
    async with wallet_operation_guard():
        try:
            # Authoritative daily-cap check: the early check above is raceable
            # across worker processes, but here the guard serializes every
            # payment, so the total cannot move between this read and the
            # melt.
            spent_24h_usd = await _ppq_spent_last_24h_usd(price)
            if spent_24h_usd + amount_usd > PPQ_MAX_DAILY_TOPUP_USD:
                logger.critical(
                    "PPQ auto top-up aborted: rolling 24h spend cap reached",
                    extra={
                        "provider_id": row.id,
                        "spent_24h_usd": round(spent_24h_usd, 2),
                        "topup_usd": amount_usd,
                        "daily_cap_usd": PPQ_MAX_DAILY_TOPUP_USD,
                    },
                )
                raise ValueError("PPQ auto top-up daily spend cap reached")

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
                amount_usd=amount_usd,
                unit=plan.unit,
                mint_url=plan.mint_url,
            )
        except Exception:
            # Nothing has been paid yet, so the claim can be handed back. If
            # the release does not land, the claim is no longer ours to reason
            # about.
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
            # The mint's own answer rules out a settlement and any reserved
            # proofs were handed back, so this claim is safe to retry next
            # cycle.
            if not await _set_ppq_state_terminal(
                row, operation_id, collected=False, swept=True
            ):
                logger.warning(
                    "Could not release the PPQ auto top-up claim after a "
                    "payment that was never attempted; it is owned by another "
                    "attempt",
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
            # Cancellation or an unexpected error after execution began is
            # also ambiguous. Preserve the claim before propagating it.
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
