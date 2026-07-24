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

        assert await db.reset_routstr_fee(session, 5_000) is True
        assert await db.reset_routstr_fee(session, 5_000) is False

        fee = await db.get_routstr_fee(session)
        await session.refresh(fee)
        assert fee.accumulated_msats == 0
        assert fee.payout_in_progress_msats == 5_000
        assert fee.total_paid_msats == 0

        assert await db.complete_routstr_fee_payout(session, 5_000) is True
        await session.refresh(fee)
        assert fee.payout_in_progress_msats == 0
        assert fee.total_paid_msats == 5_000
        assert fee.last_paid_at is not None

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

    async def prepare(*_args: object) -> Mock:
        events.append("prepare")
        return payout_wallet

    async def checkpoint(*_args: object) -> bool:
        events.append("checkpoint")
        return True

    async def send(*_args: object, **_kwargs: object) -> int:
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
    send = AsyncMock()

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
        patch("routstr.wallet.raw_send_to_lnurl", send),
        patch("routstr.wallet.logger.warning") as warning,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    send.assert_not_awaited()
    warning.assert_called_once_with("Routstr fee payout was already claimed")


@pytest.mark.asyncio
async def test_fee_payout_does_not_retry_an_unresolved_checkpoint() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=10_000,
        payout_in_progress_msats=5_000,
        payout_started_at=123,
    )

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
        patch("routstr.wallet.db.reset_routstr_fee", AsyncMock()) as checkpoint,
        patch("routstr.wallet.get_wallet", AsyncMock()) as get_wallet,
        patch("routstr.wallet.raw_send_to_lnurl", AsyncMock()) as send,
        patch("routstr.wallet.logger.critical") as critical,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    checkpoint.assert_not_awaited()
    get_wallet.assert_not_awaited()
    send.assert_not_awaited()
    critical.assert_called_once()


@pytest.mark.asyncio
async def test_fee_payout_keeps_checkpoint_when_send_outcome_is_unknown() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=5_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )
    complete = AsyncMock()

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
        patch(
            "routstr.wallet.raw_send_to_lnurl",
            AsyncMock(side_effect=TimeoutError("unknown outcome")),
        ),
        patch("routstr.wallet.logger.critical") as critical,
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    complete.assert_not_awaited()
    critical.assert_called_once()


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

    async def send(*_args: object, **_kwargs: object) -> int:
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
