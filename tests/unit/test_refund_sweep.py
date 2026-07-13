from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import CashuTransaction
from routstr.wallet import refund_sweep_once


@pytest.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'refunds.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert(
    factory: async_sessionmaker[AsyncSession], *rows: CashuTransaction
) -> None:
    async with factory() as session:
        session.add_all(rows)
        await session.commit()


async def _load(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, CashuTransaction]:
    async with factory() as session:
        result = await session.exec(select(CashuTransaction))
        return {row.token: row for row in result.all()}


@pytest.mark.asyncio
async def test_refund_sweep_only_processes_expired_eligible_outgoing_tokens(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rows = [
        CashuTransaction(
            token="eligible", amount=1, unit="sat", type="out", created_at=800
        ),
        CashuTransaction(
            token="fresh", amount=1, unit="sat", type="out", created_at=950
        ),
        CashuTransaction(
            token="boundary", amount=1, unit="sat", type="out", created_at=900
        ),
        CashuTransaction(
            token="collected",
            amount=1,
            unit="sat",
            type="out",
            created_at=800,
            collected=True,
        ),
        CashuTransaction(
            token="swept", amount=1, unit="sat", type="out", created_at=800, swept=True
        ),
        CashuTransaction(
            token="incoming", amount=1, unit="sat", type="in", created_at=800
        ),
    ]
    await _insert(session_factory, *rows)
    receive = AsyncMock()
    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.time.time", return_value=1000),
        patch("routstr.wallet.recieve_token", receive),
    ):
        await refund_sweep_once()

    receive.assert_awaited_once_with("eligible")
    loaded = await _load(session_factory)
    assert loaded["eligible"].swept is True
    assert loaded["fresh"].swept is False
    assert loaded["boundary"].swept is False
    assert loaded["collected"].swept is False
    assert loaded["swept"].swept is True
    assert loaded["incoming"].swept is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "collected"),
    [
        (RuntimeError("token already spent"), True),
        (RuntimeError("mint unavailable"), False),
    ],
)
async def test_refund_sweep_records_terminal_but_not_transient_failures(
    session_factory: async_sessionmaker[AsyncSession], error: Exception, collected: bool
) -> None:
    await _insert(
        session_factory,
        CashuTransaction(
            token="refund", amount=1, unit="sat", type="out", created_at=800
        ),
    )
    with (
        patch("routstr.wallet.db.create_session", side_effect=session_factory),
        patch("routstr.wallet.settings.refund_sweep_ttl_seconds", 100),
        patch("routstr.wallet.time.time", return_value=1000),
        patch("routstr.wallet.recieve_token", AsyncMock(side_effect=error)),
    ):
        await refund_sweep_once()

    refund = (await _load(session_factory))["refund"]
    assert refund.collected is collected
    assert refund.swept is False
