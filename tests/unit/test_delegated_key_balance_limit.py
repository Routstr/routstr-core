"""Delegated-child settlement must never exceed the child's balance limit.

A delegated key (one whose ``parent_key_hash`` points at another key) draws its
``balance`` from the parent but owns its own ``balance_limit`` and ``total_spent``.
The limit is enforced at reservation time, but settlement applies the final charge
(which can overrun the reservation when real usage exceeds the discounted
estimate) without re-checking the limit. These tests pin the exact post-settlement
state of a delegated child: an overrun must be clamped to the remaining limit,
and the parent and child must record the same clamped charge.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import (
    adjust_payment_for_tokens,
    get_reservation_snapshot,
    pay_for_request,
)
from routstr.core.db import ApiKey
from routstr.payment.cost_calculation import CostData, MaxCostData


async def _engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    return engine


def _cost(total_msats: int) -> CostData:
    return CostData(
        base_msats=0,
        input_msats=total_msats,
        output_msats=0,
        total_msats=total_msats,
    )


@pytest.mark.asyncio
async def test_an_overrun_is_clamped_to_the_childs_remaining_limit() -> None:
    engine = await _engine()
    parent = ApiKey(hashed_key="parent", balance=1_000_000)
    child = ApiKey(
        hashed_key="child",
        parent_key_hash="parent",
        balance=0,
        balance_limit=100_000,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()
        parent_before = parent.balance

        await pay_for_request(child, 10_000, session)
        snapshot = await get_reservation_snapshot(child, session)

        with patch(
            "routstr.auth.calculate_cost",
            AsyncMock(return_value=_cost(120_000)),
        ):
            await adjust_payment_for_tokens(
                child,
                {"model": "test", "usage": {}},
                session,
                10_000,
                reservation_snapshot=snapshot,
            )

        await session.refresh(parent)
        await session.refresh(child)
        assert child.balance_limit is not None
        assert child.total_spent == child.balance_limit
        assert child.reserved_balance == 0
        assert parent_before - parent.balance == child.total_spent

    await engine.dispose()


@pytest.mark.asyncio
async def test_parent_and_child_record_the_same_clamped_charge() -> None:
    engine = await _engine()
    parent = ApiKey(hashed_key="parent", balance=1_000_000)
    child = ApiKey(
        hashed_key="child",
        parent_key_hash="parent",
        balance=0,
        balance_limit=100_000,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()
        parent_before = parent.balance

        await pay_for_request(child, 10_000, session)
        snapshot = await get_reservation_snapshot(child, session)

        with patch(
            "routstr.auth.calculate_cost",
            AsyncMock(return_value=_cost(120_000)),
        ):
            await adjust_payment_for_tokens(
                child,
                {"model": "test", "usage": {}},
                session,
                10_000,
                reservation_snapshot=snapshot,
            )

        await session.refresh(parent)
        await session.refresh(child)
        assert parent_before - parent.balance == child.total_spent

    await engine.dispose()


@pytest.mark.asyncio
async def test_a_child_without_a_limit_is_charged_the_full_overrun() -> None:
    engine = await _engine()
    parent = ApiKey(hashed_key="parent", balance=1_000_000)
    child = ApiKey(hashed_key="child", parent_key_hash="parent", balance=0)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()

        await pay_for_request(child, 10_000, session)
        snapshot = await get_reservation_snapshot(child, session)

        with patch(
            "routstr.auth.calculate_cost",
            AsyncMock(return_value=_cost(120_000)),
        ):
            await adjust_payment_for_tokens(
                child,
                {"model": "test", "usage": {}},
                session,
                10_000,
                reservation_snapshot=snapshot,
            )

        await session.refresh(child)
        assert child.total_spent == 120_000

    await engine.dispose()


@pytest.mark.asyncio
async def test_a_normal_settlement_within_the_limit_is_unchanged() -> None:
    engine = await _engine()
    parent = ApiKey(hashed_key="parent", balance=1_000_000)
    child = ApiKey(
        hashed_key="child",
        parent_key_hash="parent",
        balance=0,
        balance_limit=100_000,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()

        await pay_for_request(child, 10_000, session)
        snapshot = await get_reservation_snapshot(child, session)

        with patch(
            "routstr.auth.calculate_cost",
            AsyncMock(return_value=_cost(10_000)),
        ):
            await adjust_payment_for_tokens(
                child,
                {"model": "test", "usage": {}},
                session,
                10_000,
                reservation_snapshot=snapshot,
            )

        await session.refresh(child)
        assert child.total_spent == 10_000
        assert child.reserved_balance == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_an_underrun_refunds_the_child_without_breaking_the_limit() -> None:
    engine = await _engine()
    parent = ApiKey(hashed_key="parent", balance=1_000_000)
    child = ApiKey(
        hashed_key="child",
        parent_key_hash="parent",
        balance=0,
        balance_limit=100_000,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()

        await pay_for_request(child, 10_000, session)
        snapshot = await get_reservation_snapshot(child, session)

        with patch(
            "routstr.auth.calculate_cost",
            AsyncMock(return_value=_cost(5_000)),
        ):
            await adjust_payment_for_tokens(
                child,
                {"model": "test", "usage": {}},
                session,
                10_000,
                reservation_snapshot=snapshot,
            )

        await session.refresh(child)
        assert child.total_spent == 5_000
        assert child.reserved_balance == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_a_max_cost_settlement_without_pricing_charges_the_child_nothing() -> None:
    engine = await _engine()
    parent = ApiKey(hashed_key="parent", balance=1_000_000)
    child = ApiKey(
        hashed_key="child",
        parent_key_hash="parent",
        balance=0,
        balance_limit=100_000,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()

        await pay_for_request(child, 10_000, session)
        snapshot = await get_reservation_snapshot(child, session)

        with patch(
            "routstr.auth.calculate_cost",
            AsyncMock(
                return_value=MaxCostData(
                    base_msats=0, input_msats=0, output_msats=0, total_msats=0
                )
            ),
        ):
            await adjust_payment_for_tokens(
                child,
                {"model": "test", "usage": {}},
                session,
                10_000,
                reservation_snapshot=snapshot,
            )

        await session.refresh(child)
        assert child.total_spent == 0
        assert child.reserved_balance == 0

    await engine.dispose()

@pytest.mark.asyncio
async def test_a_midflight_limit_drop_does_not_free_the_settlement() -> None:
    engine = await _engine()
    parent = ApiKey(hashed_key="parent", balance=1_000_000)
    child = ApiKey(
        hashed_key="child",
        parent_key_hash="parent",
        balance=0,
        balance_limit=100_000,
        total_spent=80_000,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add_all([parent, child])
        await session.commit()
        parent_before = parent.balance

        await pay_for_request(child, 10_000, session)
        snapshot = await get_reservation_snapshot(child, session)

        # An admin lowers the limit below total_spent + the incoming charge while
        # the reservation is still in flight.
        child.balance_limit = 83_000
        session.add(child)
        await session.commit()

        with patch(
            "routstr.auth.calculate_cost",
            AsyncMock(return_value=_cost(4_000)),
        ):
            await adjust_payment_for_tokens(
                child,
                {"model": "test", "usage": {}},
                session,
                10_000,
                reservation_snapshot=snapshot,
            )

        await session.refresh(parent)
        await session.refresh(child)
        assert parent_before - parent.balance == 4_000
        assert child.total_spent == 84_000
        assert child.reserved_balance == 0

    await engine.dispose()

