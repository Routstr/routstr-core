import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr import wallet
from routstr.core import db
from routstr.payment.lnurl import LNURLError


class _SessionContext:
    def __init__(self, session: Mock) -> None:
        self.session = session

    async def __aenter__(self) -> Mock:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        return None


def _session_context(session: Mock) -> _SessionContext:
    return _SessionContext(session)


@pytest.mark.asyncio
async def test_fee_payout_checkpoint_is_atomic_and_durable() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(engine) as session:
        session.add(db.RoutstrFee(id=1, accumulated_msats=5_000))
        await session.commit()

        assert (
            await db.reset_routstr_fee(
                session, 5_000, "quote-1", "https://mint.test", "sat"
            )
            is True
        )
        assert (
            await db.reset_routstr_fee(
                session, 5_000, "quote-2", "https://mint.test", "sat"
            )
            is False
        )

        fee = await db.get_routstr_fee(session)
        await session.refresh(fee)
        assert fee.accumulated_msats == 0
        assert fee.payout_in_progress_msats == 5_000
        assert fee.payout_quote_id == "quote-1"
        assert fee.payout_mint_url == "https://mint.test"
        assert fee.payout_unit == "sat"
        assert fee.total_paid_msats == 0

        assert (
            await db.complete_routstr_fee_payout(
                session, 5_000, "quote-1", "https://mint.test", "sat"
            )
            is True
        )
        await session.refresh(fee)
        assert fee.payout_in_progress_msats == 0
        assert fee.payout_quote_id is None
        assert fee.payout_mint_url is None
        assert fee.payout_unit is None
        assert fee.total_paid_msats == 5_000
        assert fee.last_paid_at is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_fee_payout_checkpoint_can_be_restored_for_retry() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(engine) as session:
        session.add(db.RoutstrFee(id=1, accumulated_msats=7_000))
        await session.commit()

        assert (
            await db.reset_routstr_fee(
                session, 5_000, "quote-1", "https://mint.test", "sat"
            )
            is True
        )
        assert (
            await db.restore_routstr_fee_payout(
                session, 5_000, "quote-1", "https://mint.test", "sat"
            )
            is True
        )
        assert (
            await db.restore_routstr_fee_payout(
                session, 5_000, "quote-1", "https://mint.test", "sat"
            )
            is False
        )

        fee = await db.get_routstr_fee(session)
        await session.refresh(fee)
        assert fee.accumulated_msats == 7_000
        assert fee.payout_in_progress_msats == 0
        assert fee.payout_started_at is None
        assert fee.payout_quote_id is None
        assert fee.payout_mint_url is None
        assert fee.payout_unit is None
        assert fee.total_paid_msats == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_reconciliation_cannot_mutate_replacement_quote() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(engine) as session:
        session.add(db.RoutstrFee(id=1, accumulated_msats=10_000))
        await session.commit()

        assert await db.reset_routstr_fee(
            session, 5_000, "quote-1", "https://mint.test", "sat"
        )
        assert await db.restore_routstr_fee_payout(
            session, 5_000, "quote-1", "https://mint.test", "sat"
        )
        assert await db.reset_routstr_fee(
            session, 5_000, "quote-2", "https://mint.test", "sat"
        )

        assert not await db.restore_routstr_fee_payout(
            session, 5_000, "quote-1", "https://mint.test", "sat"
        )
        assert not await db.complete_routstr_fee_payout(
            session, 5_000, "quote-1", "https://mint.test", "sat"
        )

        fee = await db.get_routstr_fee(session)
        await session.refresh(fee)
        assert fee.accumulated_msats == 5_000
        assert fee.payout_in_progress_msats == 5_000
        assert fee.payout_quote_id == "quote-2"
        assert fee.total_paid_msats == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_fee_payout_prepares_wallet_then_checkpoints_before_sending() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=5_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )
    payout_wallet = Mock()
    events: list[str] = []

    async def prepare(*_args: object, **_kwargs: object) -> Mock:
        events.append("prepare")
        return payout_wallet

    async def checkpoint(*_args: object) -> bool:
        events.append("checkpoint")
        return True

    async def send(*_args: object, **kwargs: object) -> int:
        checkpoint_quote = kwargs["on_melt_quote"]
        await checkpoint_quote("quote-1")  # type: ignore[operator]
        events.append("send")
        return 5

    async def complete(*_args: object) -> bool:
        events.append("complete")
        return True

    with (
        patch("routstr.auth.ROUTSTR_FEE_DEFAULT_PAYOUT", 1),
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.reset_routstr_fee", side_effect=checkpoint),
        patch("routstr.wallet.db.complete_routstr_fee_payout", side_effect=complete),
        patch("routstr.wallet.get_wallet", AsyncMock(side_effect=prepare)),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[]),
        patch("routstr.wallet.raw_send_to_lnurl", side_effect=send),
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    assert events == ["prepare", "checkpoint", "send", "complete"]


@pytest.mark.asyncio
async def test_fee_payout_preparation_failure_does_not_checkpoint() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=5_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )
    checkpoint = AsyncMock()

    with (
        patch("routstr.auth.ROUTSTR_FEE_DEFAULT_PAYOUT", 1),
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.reset_routstr_fee", checkpoint),
        patch(
            "routstr.wallet.get_wallet",
            AsyncMock(side_effect=RuntimeError("wallet unavailable")),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    checkpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_fee_payout_lost_checkpoint_race_does_not_send() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=5_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )
    dispatched = AsyncMock()

    async def send(*_args: object, **kwargs: object) -> int:
        checkpoint_quote = kwargs["on_melt_quote"]
        await checkpoint_quote("quote-1")  # type: ignore[operator]
        await dispatched()
        return 5

    with (
        patch("routstr.auth.ROUTSTR_FEE_DEFAULT_PAYOUT", 1),
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch(
            "routstr.wallet.db.reset_routstr_fee",
            AsyncMock(return_value=False),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=Mock())),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[]),
        patch("routstr.wallet.raw_send_to_lnurl", side_effect=send),
        patch("routstr.wallet.logger.warning") as warning,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    dispatched.assert_not_awaited()
    warning.assert_called_once_with("Routstr fee payout was already claimed")


@pytest.mark.asyncio
async def test_fee_payout_finalizes_a_paid_unresolved_quote_without_resending() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=10_000,
        payout_in_progress_msats=5_000,
        payout_started_at=123,
        payout_quote_id="quote-1",
        payout_mint_url="https://mint.test",
        payout_unit="sat",
    )
    complete = AsyncMock(return_value=True)
    restore = AsyncMock()
    send = AsyncMock()

    with (
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.complete_routstr_fee_payout", complete),
        patch("routstr.wallet.db.restore_routstr_fee_payout", restore),
        patch(
            "routstr.wallet._check_bolt11_payment_status_locked",
            AsyncMock(return_value="paid"),
        ) as status,
        patch("routstr.wallet.raw_send_to_lnurl", send),
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    status.assert_awaited_once_with("https://mint.test", "sat", "quote-1")
    complete.assert_awaited_once_with(
        session, 5_000, "quote-1", "https://mint.test", "sat"
    )
    restore.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_fee_payout_restores_only_an_unpaid_quote_and_retries() -> None:
    session = Mock()
    unresolved_fee = SimpleNamespace(
        accumulated_msats=10_000,
        payout_in_progress_msats=5_000,
        payout_started_at=123,
        payout_quote_id="quote-1",
        payout_mint_url="https://mint.test",
        payout_unit="sat",
    )
    restored_fee = SimpleNamespace(
        accumulated_msats=15_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )

    async def send(*_args: object, **kwargs: object) -> int:
        await kwargs["on_melt_quote"]("quote-2")  # type: ignore[index,operator]
        return 15

    with (
        patch("routstr.auth.ROUTSTR_FEE_DEFAULT_PAYOUT", 1),
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch(
            "routstr.wallet.db.get_routstr_fee",
            AsyncMock(side_effect=[unresolved_fee, unresolved_fee, restored_fee]),
        ),
        patch(
            "routstr.wallet._check_bolt11_payment_status_locked",
            AsyncMock(return_value="unpaid"),
        ),
        patch(
            "routstr.wallet.db.restore_routstr_fee_payout",
            AsyncMock(return_value=True),
        ) as restore,
        patch(
            "routstr.wallet.db.reset_routstr_fee", AsyncMock(return_value=True)
        ) as reset,
        patch(
            "routstr.wallet.db.complete_routstr_fee_payout",
            AsyncMock(return_value=True),
        ),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=Mock())),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[]),
        patch("routstr.wallet.raw_send_to_lnurl", side_effect=send) as raw_send,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    restore.assert_awaited_once_with(
        session, 5_000, "quote-1", "https://mint.test", "sat"
    )
    reset.assert_awaited_once_with(
        session, 15_000, "quote-2", wallet.settings.primary_mint, "sat"
    )
    raw_send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_state", ["pending", "unknown"])
async def test_fee_payout_keeps_nonfinal_quote_locked(quote_state: str) -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=10_000,
        payout_in_progress_msats=5_000,
        payout_started_at=123,
        payout_quote_id="quote-1",
        payout_mint_url="https://mint.test",
        payout_unit="sat",
    )
    complete = AsyncMock()
    restore = AsyncMock()

    with (
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.complete_routstr_fee_payout", complete),
        patch("routstr.wallet.db.restore_routstr_fee_payout", restore),
        patch(
            "routstr.wallet._check_bolt11_payment_status_locked",
            AsyncMock(return_value=quote_state),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    complete.assert_not_awaited()
    restore.assert_not_awaited()


@pytest.mark.asyncio
async def test_fee_payout_reconciliation_rechecks_state_under_wallet_guard() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=10_000,
        payout_in_progress_msats=5_000,
        payout_started_at=123,
        payout_quote_id="quote-1",
        payout_mint_url="https://mint.test",
        payout_unit="sat",
    )
    guard_held = False

    @asynccontextmanager
    async def guard() -> AsyncGenerator[None, None]:
        nonlocal guard_held
        assert not guard_held
        guard_held = True
        try:
            yield
        finally:
            guard_held = False

    async def status(*_args: object) -> str:
        assert guard_held
        return "pending"

    get_fee = AsyncMock(return_value=fee)
    with (
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", get_fee),
        patch("routstr.wallet.wallet_operation_guard", guard),
        patch(
            "routstr.wallet._check_bolt11_payment_status_locked",
            side_effect=status,
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    assert get_fee.await_count == 2
    assert not guard_held


@pytest.mark.asyncio
async def test_fee_payout_keeps_legacy_checkpoint_without_quote_locked() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=10_000,
        payout_in_progress_msats=5_000,
        payout_started_at=123,
        payout_quote_id=None,
        payout_mint_url=None,
        payout_unit=None,
    )
    restore = AsyncMock()

    with (
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.restore_routstr_fee_payout", restore),
        patch("routstr.wallet.logger.critical") as critical,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    restore.assert_not_awaited()
    critical.assert_called_once()


@pytest.mark.asyncio
async def test_fee_payout_failure_before_quote_is_not_reported_as_unknown() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=1_061_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )
    reset = AsyncMock()

    with (
        patch("routstr.auth.ROUTSTR_FEE_DEFAULT_PAYOUT", 1),
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.reset_routstr_fee", reset),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=Mock())),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[]),
        patch(
            "routstr.wallet.raw_send_to_lnurl",
            side_effect=LNURLError("Cashu melt fees leave no payable LNURL amount"),
        ),
        patch("routstr.wallet.logger.error") as error,
        patch("routstr.wallet.logger.critical") as critical,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    reset.assert_not_awaited()
    critical.assert_not_called()
    error.assert_called_once()
    assert error.call_args.args[0] == "Routstr fee payout failed before melt dispatch"


@pytest.mark.asyncio
async def test_fee_payout_keeps_checkpoint_when_send_outcome_is_unknown() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=5_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )
    complete = AsyncMock()

    async def send(*_args: object, **kwargs: object) -> int:
        checkpoint_quote = kwargs["on_melt_quote"]
        await checkpoint_quote("quote-1")  # type: ignore[operator]
        raise TimeoutError("unknown outcome")

    with (
        patch("routstr.auth.ROUTSTR_FEE_DEFAULT_PAYOUT", 1),
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.reset_routstr_fee", AsyncMock(return_value=True)),
        patch("routstr.wallet.db.complete_routstr_fee_payout", complete),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=Mock())),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[]),
        patch("routstr.wallet.raw_send_to_lnurl", side_effect=send),
        patch("routstr.wallet.logger.critical") as critical,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    complete.assert_not_awaited()
    critical.assert_called_once()


@pytest.mark.asyncio
async def test_fee_payout_cancellation_during_send_alerts_and_propagates() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=5_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )
    complete = AsyncMock()

    async def cancel_send(*_args: object, **kwargs: object) -> int:
        checkpoint_quote = kwargs["on_melt_quote"]
        await checkpoint_quote("quote-1")  # type: ignore[operator]
        raise asyncio.CancelledError

    with (
        patch("routstr.auth.ROUTSTR_FEE_DEFAULT_PAYOUT", 1),
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch("routstr.wallet.asyncio.sleep", AsyncMock(return_value=None)),
        patch(
            "routstr.wallet.db.create_session", return_value=_session_context(session)
        ),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.reset_routstr_fee", AsyncMock(return_value=True)),
        patch("routstr.wallet.db.complete_routstr_fee_payout", complete),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=Mock())),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[]),
        patch("routstr.wallet.raw_send_to_lnurl", side_effect=cancel_send),
        patch("routstr.wallet.logger.critical") as critical,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    complete.assert_not_awaited()
    critical.assert_called_once()
    assert critical.call_args.args[0] == (
        "Routstr fee payout outcome is unknown; awaiting quote reconciliation"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["session", "completion"])
async def test_fee_payout_completion_failures_use_sent_checkpoint_alert(
    failure_site: str,
) -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=5_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )
    completion = AsyncMock()
    if failure_site == "session":
        create_session = Mock(
            side_effect=[
                _session_context(session),
                _session_context(session),
                RuntimeError("pool unavailable"),
            ]
        )
    else:
        create_session = Mock(return_value=_session_context(session))
        completion.side_effect = RuntimeError("checkpoint unavailable")

    async def send(*_args: object, **kwargs: object) -> int:
        checkpoint_quote = kwargs["on_melt_quote"]
        await checkpoint_quote("quote-1")  # type: ignore[operator]
        return 5

    with (
        patch("routstr.auth.ROUTSTR_FEE_DEFAULT_PAYOUT", 1),
        patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
        patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
        patch(
            "routstr.wallet.asyncio.sleep",
            AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ),
        patch("routstr.wallet.db.create_session", create_session),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.reset_routstr_fee", AsyncMock(return_value=True)),
        patch("routstr.wallet.db.complete_routstr_fee_payout", completion),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=Mock())),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[]),
        patch("routstr.wallet.raw_send_to_lnurl", side_effect=send),
        patch("routstr.wallet.logger.critical") as critical,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    critical.assert_called_once()
    assert critical.call_args.args[0] == (
        "Routstr fee payout sent but checkpoint was not completed; "
        "awaiting quote reconciliation"
    )


@pytest.mark.asyncio
async def test_fee_payout_releases_db_connection_during_send(tmp_path: object) -> None:
    """With pool_size=1, the payout must not hold a connection while the
    external LNURL send is in flight, or the completion step would starve."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/payout.db", pool_size=1, max_overflow=0
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(engine) as session:
        session.add(db.RoutstrFee(id=1, accumulated_msats=5_000_000))
        await session.commit()

    @asynccontextmanager
    async def create_session() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    async def send(*_args: object, **kwargs: object) -> int:
        assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        checkpoint_quote = kwargs["on_melt_quote"]
        await checkpoint_quote("quote-1")  # type: ignore[operator]
        assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        return 5

    try:
        with (
            patch("routstr.auth.ROUTSTR_FEE_DEFAULT_PAYOUT", 1),
            patch("routstr.auth.ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS", 1),
            patch("routstr.auth.ROUTSTR_LN_ADDRESS", "fees@example.com"),
            patch(
                "routstr.wallet.asyncio.sleep",
                AsyncMock(side_effect=[None, asyncio.CancelledError()]),
            ),
            patch("routstr.wallet.db.create_session", create_session),
            patch("routstr.wallet.get_wallet", AsyncMock(return_value=Mock())),
            patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[]),
            patch("routstr.wallet.raw_send_to_lnurl", side_effect=send),
        ):
            with pytest.raises(asyncio.CancelledError):
                await wallet.periodic_routstr_fee_payout()

        async with AsyncSession(engine) as session:
            fee = await db.get_routstr_fee(session)
            assert fee.payout_in_progress_msats == 0
            assert fee.total_paid_msats == 5_000_000
    finally:
        await engine.dispose()
