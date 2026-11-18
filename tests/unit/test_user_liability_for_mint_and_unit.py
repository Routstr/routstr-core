"""Real-DB coverage for the per-mint liability query that bounds owner payout."""

from typing import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import (
    ApiKey,
    CashuTransaction,
    Refund,
    total_user_liability,
    user_liability_for_mint_and_unit,
)

MINT = "http://m1"


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
    mint_url: str | None = MINT,
    currency: str | None = "sat",
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


async def _add_refund(
    session: AsyncSession,
    hashed_key: str,
    amount_msats: int,
    status: str,
    mint_url: str = MINT,
    unit: str = "sat",
) -> None:
    session.add(
        Refund(
            api_key_hashed_key=hashed_key,
            method="lightning",
            amount_msats=amount_msats,
            unit=unit,
            mint_url=mint_url,
            status=status,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_sums_key_balances_for_the_mint_and_unit(session: AsyncSession) -> None:
    await _add_key(session, "a", 1000)
    await _add_key(session, "b", 500)

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 1500


@pytest.mark.asyncio
async def test_adds_unresolved_refunds_to_key_balances(session: AsyncSession) -> None:
    # Only one pending/ambiguous claim per key is allowed.
    await _add_key(session, "a", 1000)
    await _add_refund(session, "a", 300, "pending")
    await _add_key(session, "b", 0)
    await _add_refund(session, "b", 40, "ambiguous")
    await _add_key(session, "c", 0)
    await _add_refund(session, "c", 7, "stuck")

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 1347


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paid", "failed"])
async def test_excludes_resolved_refunds(session: AsyncSession, status: str) -> None:
    await _add_key(session, "a", 0)
    await _add_refund(session, "a", 900, status)

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 0


@pytest.mark.asyncio
async def test_excludes_other_mints_and_units(session: AsyncSession) -> None:
    await _add_key(session, "a", 1000)
    await _add_key(session, "other-mint", 111, mint_url="http://m2")
    await _add_key(session, "other-unit", 222, currency="msat")
    await _add_refund(session, "a", 300, "pending")
    await _add_refund(session, "other-mint", 444, "pending", mint_url="http://m2")
    await _add_refund(session, "other-unit", 555, "pending", unit="msat")

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 1300


@pytest.mark.asyncio
async def test_excludes_keys_without_a_refund_mint(session: AsyncSession) -> None:
    await _add_key(session, "a", 1000)
    await _add_key(session, "unattributed", 4242, mint_url=None, currency=None)

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 1000


@pytest.mark.asyncio
async def test_unknown_mint_has_no_liability(session: AsyncSession) -> None:
    await _add_key(session, "a", 1000)
    await _add_refund(session, "a", 300, "pending")

    assert await user_liability_for_mint_and_unit(session, "http://missing", "sat") == 0


async def _add_x_cashu_payment(
    session: AsyncSession,
    request_id: str,
    amount: int,
    settlement: str | None,
    unit: str = "sat",
    mint_url: str = MINT,
    refund_amount: int | None = None,
) -> None:
    session.add(
        CashuTransaction(
            token=f"cashuA{request_id}",
            amount=amount,
            unit=unit,
            mint_url=mint_url,
            type="in",
            request_id=request_id,
            collected=True,
            settlement=settlement,
            refund_amount=refund_amount,
        )
    )
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", ["pending", "refunding", "failed"])
async def test_counts_unsettled_x_cashu_payments(
    session: AsyncSession, settlement: str
) -> None:
    # A payment left unsettled by a restart is refunded in full by recovery,
    # and a failed refund is still owed until reconciled, so the payout must
    # not treat its proofs as owner surplus.
    await _add_key(session, "a", 1000)
    await _add_x_cashu_payment(session, "r1", 60, settlement)
    await _add_x_cashu_payment(session, "r2", 5000, settlement, unit="msat")

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 61_000
    assert await user_liability_for_mint_and_unit(session, MINT, "msat") == 5000
    assert await total_user_liability(session) == 66_000


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", ["refunding", "failed"])
async def test_counts_only_the_outstanding_x_cashu_refund(
    session: AsyncSession, settlement: str
) -> None:
    await _add_x_cashu_payment(session, "r1", 20, settlement, refund_amount=15)

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 15_000
    assert await total_user_liability(session) == 15_000


@pytest.mark.asyncio
async def test_pending_x_cashu_payment_stays_fully_owed(session: AsyncSession) -> None:
    await _add_x_cashu_payment(session, "r1", 20, "pending", refund_amount=15)

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 20_000
    assert await total_user_liability(session) == 20_000


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", ["charged", "refunded", None])
async def test_excludes_settled_x_cashu_payments(
    session: AsyncSession, settlement: str | None
) -> None:
    await _add_x_cashu_payment(session, "r1", 60, settlement)

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 0
    assert await total_user_liability(session) == 0


@pytest.mark.asyncio
async def test_unsettled_x_cashu_payments_on_other_mints_are_excluded(
    session: AsyncSession,
) -> None:
    await _add_x_cashu_payment(session, "r1", 60, "pending", mint_url="http://m2")

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 0
    assert await total_user_liability(session) == 60_000


@pytest.mark.asyncio
async def test_large_x_cashu_payment_does_not_overflow(session: AsyncSession) -> None:
    # 3M sats * 1000 exceeds a 32-bit INTEGER on PostgreSQL.
    await _add_x_cashu_payment(session, "r1", 3_000_000, "pending")

    assert await user_liability_for_mint_and_unit(session, MINT, "sat") == 3 * 10**9
    assert await total_user_liability(session) == 3 * 10**9
