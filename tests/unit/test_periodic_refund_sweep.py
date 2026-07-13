from contextlib import asynccontextmanager
from typing import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr import wallet
from routstr.core.db import CashuTransaction


def _make_engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
async def refund_db(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncEngine, None]:
    engine = _make_engine()
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def create_session() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(wallet.db, "create_session", create_session)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _store(
    engine: AsyncEngine,
    *,
    token: str,
    created_at: int,
    typ: str = "out",
    collected: bool = False,
    swept: bool = False,
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            CashuTransaction(
                token=token,
                amount=10,
                unit="sat",
                type=typ,
                created_at=created_at,
                collected=collected,
                swept=swept,
            )
        )
        await session.commit()


async def _transactions(engine: AsyncEngine) -> dict[str, CashuTransaction]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        results = await session.exec(select(CashuTransaction))
        return {transaction.token: transaction for transaction in results.all()}


@pytest.mark.asyncio
async def test_refund_sweep_only_processes_eligible_outbound_transactions(
    refund_db: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cutoff = 1_000
    await _store(refund_db, token="eligible", created_at=cutoff - 1)
    await _store(refund_db, token="too-new", created_at=cutoff)
    await _store(
        refund_db, token="collected", created_at=cutoff - 1, collected=True
    )
    await _store(refund_db, token="swept", created_at=cutoff - 1, swept=True)
    await _store(refund_db, token="inbound", created_at=cutoff - 1, typ="in")
    receive_token = AsyncMock()
    monkeypatch.setattr(wallet, "recieve_token", receive_token)

    await wallet._refund_sweep_once(cutoff)

    receive_token.assert_awaited_once_with("eligible")
    transactions = await _transactions(refund_db)
    assert transactions["eligible"].swept is True
    assert transactions["too-new"].swept is False
    assert transactions["collected"].collected is True
    assert transactions["collected"].swept is False
    assert transactions["swept"].swept is True
    assert transactions["inbound"].swept is False


@pytest.mark.asyncio
async def test_refund_sweep_persists_success_and_isolates_token_failures(
    refund_db: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    cutoff = 1_000
    for token in ("success", "already-spent", "temporary-failure"):
        await _store(refund_db, token=token, created_at=cutoff - 1)

    async def receive_token(token: str) -> None:
        if token == "already-spent":
            raise RuntimeError("Token already spent")
        if token == "temporary-failure":
            raise RuntimeError("mint unavailable")

    receive = AsyncMock(side_effect=receive_token)
    monkeypatch.setattr(wallet, "recieve_token", receive)

    await wallet._refund_sweep_once(cutoff)

    assert receive.await_count == 3
    transactions = await _transactions(refund_db)
    assert transactions["success"].swept is True
    assert transactions["success"].collected is False
    assert transactions["already-spent"].collected is True
    assert transactions["already-spent"].swept is False
    assert transactions["temporary-failure"].collected is False
    assert transactions["temporary-failure"].swept is False
