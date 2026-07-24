"""Real-DB coverage for db.balances_by_mint_and_unit.

Verifies the grouped liability query used by fetch_all_balances: it sums
balances per (mint_url, unit), filters to the requested mints/units, excludes
NULL mint/currency rows, and returns nothing for empty inputs.
"""

from typing import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey, balances_by_mint_and_unit


def _make_engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
async def session() -> "AsyncGenerator[AsyncSession, None]":
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    db_session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db_session
    finally:
        await db_session.close()
        await engine.dispose()


async def _add_key(
    session: AsyncSession,
    hashed_key: str,
    balance: int,
    mint_url: str | None,
    currency: str | None,
) -> None:
    session.add(
        ApiKey(
            hashed_key=hashed_key,
            balance=balance,
            refund_mint_url=mint_url,
            refund_currency=currency,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_sums_and_groups_by_mint_and_unit(session: AsyncSession) -> None:
    await _add_key(session, "a", 1000, "http://m1", "sat")
    await _add_key(session, "b", 500, "http://m1", "sat")
    await _add_key(session, "c", 7000, "http://m1", "msat")
    await _add_key(session, "d", 200, "http://m2", "sat")

    result = await balances_by_mint_and_unit(
        session, ["http://m1", "http://m2"], ["sat", "msat"]
    )

    assert result[("http://m1", "sat")] == 1500
    assert result[("http://m1", "msat")] == 7000
    assert result[("http://m2", "sat")] == 200


@pytest.mark.asyncio
async def test_filters_out_unrequested_mints_and_units(session: AsyncSession) -> None:
    await _add_key(session, "a", 1000, "http://wanted", "sat")
    await _add_key(session, "b", 999, "http://other", "sat")
    await _add_key(session, "c", 888, "http://wanted", "usd")

    result = await balances_by_mint_and_unit(session, ["http://wanted"], ["sat"])

    assert result == {("http://wanted", "sat"): 1000}


@pytest.mark.asyncio
async def test_excludes_rows_with_null_mint_or_currency(session: AsyncSession) -> None:
    await _add_key(session, "a", 1000, "http://m1", "sat")
    await _add_key(session, "b", 4242, None, None)

    result = await balances_by_mint_and_unit(session, ["http://m1"], ["sat"])

    assert result == {("http://m1", "sat"): 1000}


@pytest.mark.asyncio
async def test_empty_inputs_return_empty_mapping(session: AsyncSession) -> None:
    await _add_key(session, "a", 1000, "http://m1", "sat")

    assert await balances_by_mint_and_unit(session, [], ["sat"]) == {}
    assert await balances_by_mint_and_unit(session, ["http://m1"], []) == {}
