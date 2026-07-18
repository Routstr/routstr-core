import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr import wallet
from routstr.core import db


@asynccontextmanager
async def _session_context(session: Mock) -> AsyncIterator[Mock]:
    yield session


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
async def test_fee_payout_checkpoints_before_sending() -> None:
    session = Mock()
    fee = SimpleNamespace(
        accumulated_msats=5_000,
        payout_in_progress_msats=0,
        payout_started_at=None,
    )
    payout_wallet = Mock()
    events: list[str] = []

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
        patch("routstr.wallet.db.create_session", return_value=_session_context(session)),
        patch("routstr.wallet.db.get_routstr_fee", AsyncMock(return_value=fee)),
        patch("routstr.wallet.db.reset_routstr_fee", side_effect=checkpoint),
        patch("routstr.wallet.db.complete_routstr_fee_payout", side_effect=complete),
        patch("routstr.wallet.get_wallet", AsyncMock(return_value=payout_wallet)),
        patch("routstr.wallet.get_proofs_per_mint_and_unit", return_value=[]),
        patch("routstr.wallet.raw_send_to_lnurl", side_effect=send),
    ):
        with pytest.raises(asyncio.CancelledError):
            await wallet.periodic_routstr_fee_payout()

    assert events == ["checkpoint", "send", "complete"]


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
        patch("routstr.wallet.db.create_session", return_value=_session_context(session)),
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
