from __future__ import annotations

from typing import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey
from routstr.upstream.ehbp import (
    finalize_ehbp_actual_cost_payment,
    finalize_ehbp_max_cost_payment,
)


def _make_engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
async def session(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[AsyncSession, None]:
    monkeypatch.setattr("routstr.upstream.ehbp.ROUTSTR_FEE_PERCENT", 0)
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    db_session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db_session
    finally:
        await db_session.close()
        await engine.dispose()


async def _api_key(session: AsyncSession, hashed_key: str) -> ApiKey | None:
    return (
        await session.exec(select(ApiKey).where(ApiKey.hashed_key == hashed_key))
    ).one_or_none()


@pytest.mark.asyncio
async def test_finalize_actual_cost_payment_updates_balance_and_releases_reserve(
    session: AsyncSession,
) -> None:
    key = ApiKey(
        hashed_key="ehbp-actual",
        balance=10_000,
        reserved_balance=3_000,
        reserved_at=123,
    )
    session.add(key)
    await session.commit()

    await finalize_ehbp_actual_cost_payment(
        key,
        session,
        reserved_cost_for_model=3_000,
        model_id="tinfoil/model",
        cost_info={
            "total_msats": 1_200,
            "input_tokens": 10,
            "output_tokens": 20,
            "input_msats": 500,
            "output_msats": 700,
        },
    )

    updated = await _api_key(session, "ehbp-actual")
    assert updated is not None
    assert updated.balance == 8_800
    assert updated.reserved_balance == 0
    assert updated.reserved_at is None
    assert updated.total_spent == 1_200


@pytest.mark.asyncio
async def test_finalize_max_cost_payment_updates_parent_and_child_spend(
    session: AsyncSession,
) -> None:
    parent = ApiKey(
        hashed_key="ehbp-parent",
        balance=10_000,
        reserved_balance=3_000,
        reserved_at=123,
    )
    child = ApiKey(
        hashed_key="ehbp-child",
        balance=0,
        reserved_balance=3_000,
        reserved_at=123,
        parent_key_hash="ehbp-parent",
    )
    session.add(parent)
    session.add(child)
    await session.commit()

    await finalize_ehbp_max_cost_payment(
        child,
        session,
        max_cost_for_model=3_000,
        model_id="tinfoil/model",
    )

    updated_parent = await _api_key(session, "ehbp-parent")
    updated_child = await _api_key(session, "ehbp-child")
    assert updated_parent is not None
    assert updated_child is not None
    assert updated_parent.balance == 7_000
    assert updated_parent.reserved_balance == 0
    assert updated_parent.reserved_at is None
    assert updated_parent.total_spent == 3_000
    assert updated_child.balance == 0
    assert updated_child.reserved_balance == 0
    assert updated_child.reserved_at is None
    assert updated_child.total_spent == 3_000


@pytest.mark.asyncio
async def test_finalize_actual_cost_payment_rolls_back_when_parent_update_matches_no_rows(
    session: AsyncSession,
) -> None:
    key = ApiKey(
        hashed_key="ehbp-missing-parent",
        balance=10_000,
        reserved_balance=3_000,
        reserved_at=123,
    )
    session.add(key)
    await session.commit()
    await session.delete(key)
    await session.commit()

    await finalize_ehbp_actual_cost_payment(
        key,
        session,
        reserved_cost_for_model=3_000,
        model_id="tinfoil/model",
        cost_info={"total_msats": 1_200},
    )

    assert await _api_key(session, "ehbp-missing-parent") is None


@pytest.mark.asyncio
async def test_finalize_max_cost_payment_rolls_back_parent_when_child_update_matches_no_rows(
    session: AsyncSession,
) -> None:
    parent = ApiKey(
        hashed_key="ehbp-rollback-parent",
        balance=10_000,
        reserved_balance=3_000,
        reserved_at=123,
    )
    child = ApiKey(
        hashed_key="ehbp-missing-child",
        balance=0,
        reserved_balance=3_000,
        reserved_at=123,
        parent_key_hash="ehbp-rollback-parent",
    )
    session.add(parent)
    session.add(child)
    await session.commit()
    await session.delete(child)
    await session.commit()

    await finalize_ehbp_max_cost_payment(
        child,
        session,
        max_cost_for_model=3_000,
        model_id="tinfoil/model",
    )

    updated_parent = await _api_key(session, "ehbp-rollback-parent")
    assert updated_parent is not None
    assert updated_parent.balance == 10_000
    assert updated_parent.reserved_balance == 3_000
    assert updated_parent.reserved_at == 123
    assert updated_parent.total_spent == 0
    assert await _api_key(session, "ehbp-missing-child") is None
