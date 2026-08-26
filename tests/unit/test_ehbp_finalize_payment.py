from __future__ import annotations

from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, col, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

import routstr.auth as auth_module
from routstr.auth import get_reservation_snapshot, pay_for_request
from routstr.core.db import ApiKey, ReservationRelease
from routstr.upstream.ehbp import (
    _inject_cost_response_headers,
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
async def session(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncSession, None]:
    monkeypatch.setattr("routstr.upstream.ehbp.ROUTSTR_FEE_PERCENT", 0)
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    db_session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db_session
    finally:
        for release_id in list(auth_module._reservation_heartbeats):
            await auth_module._stop_reservation_heartbeat(release_id)
        await db_session.close()
        await engine.dispose()


async def _api_key(session: AsyncSession, hashed_key: str) -> ApiKey | None:
    return (
        await session.exec(select(ApiKey).where(ApiKey.hashed_key == hashed_key))
    ).one_or_none()


def _fail_nth_api_key_update(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    target_update: int,
) -> None:
    """Return rowcount=0 for one API-key UPDATE without mutating the database."""
    original_exec = session.exec
    api_key_updates = 0

    async def exec_with_failure(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal api_key_updates
        table = getattr(statement, "table", None)
        if getattr(table, "name", None) == "api_keys":
            api_key_updates += 1
            if api_key_updates == target_update:
                return MagicMock(rowcount=0)
        return await original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(session, "exec", exec_with_failure)


@pytest.mark.asyncio
async def test_finalize_actual_cost_payment_updates_balance_and_releases_reserve(
    session: AsyncSession,
) -> None:
    key = ApiKey(hashed_key="ehbp-actual", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)

    charged = await finalize_ehbp_actual_cost_payment(
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
        reservation_snapshot=reservation,
    )

    assert charged == 1_200
    updated = await _api_key(session, "ehbp-actual")
    assert updated is not None
    assert updated.balance == 8_800
    assert updated.reserved_balance == 0
    assert updated.reserved_at is None
    assert updated.total_spent == 1_200


@pytest.mark.asyncio
async def test_unmeasured_ehbp_releases_parent_and_child_reservation(
    session: AsyncSession,
) -> None:
    parent = ApiKey(hashed_key="ehbp-parent", balance=10_000)
    child = ApiKey(hashed_key="ehbp-child", balance=0, parent_key_hash="ehbp-parent")
    session.add(parent)
    session.add(child)
    await session.commit()
    await pay_for_request(child, 3_000, session)
    reservation = await get_reservation_snapshot(child, session)

    charged = await finalize_ehbp_max_cost_payment(
        child,
        session,
        max_cost_for_model=3_000,
        model_id="tinfoil/model",
        reservation_snapshot=reservation,
    )

    assert charged == 0
    updated_parent = await _api_key(session, "ehbp-parent")
    updated_child = await _api_key(session, "ehbp-child")
    assert updated_parent is not None
    assert updated_child is not None
    assert updated_parent.balance == 10_000
    assert updated_parent.reserved_balance == 0
    assert updated_parent.reserved_at is None
    assert updated_parent.total_spent == 0
    assert updated_child.balance == 0
    assert updated_child.reserved_balance == 0
    assert updated_child.reserved_at is None
    assert updated_child.total_spent == 0


@pytest.mark.asyncio
async def test_finalize_actual_cost_payment_rolls_back_when_parent_update_matches_no_rows(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ApiKey(hashed_key="ehbp-missing-parent", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)
    _fail_nth_api_key_update(session, monkeypatch, target_update=1)
    rollback_spy = AsyncMock(wraps=session.rollback)
    monkeypatch.setattr(session, "rollback", rollback_spy)

    charged = await finalize_ehbp_actual_cost_payment(
        key,
        session,
        reserved_cost_for_model=3_000,
        model_id="tinfoil/model",
        cost_info={"total_msats": 1_200},
        reservation_snapshot=reservation,
    )

    assert charged == 0
    rollback_spy.assert_awaited_once()
    updated = await _api_key(session, "ehbp-missing-parent")
    assert updated is not None
    assert updated.balance == 10_000
    assert updated.reserved_balance == 0
    assert updated.total_spent == 0
    release = await session.get(ReservationRelease, reservation.release_id)
    assert release is not None
    assert release.status == "released"
    assert reservation.release_id not in auth_module._reservation_heartbeats


@pytest.mark.asyncio
async def test_unmeasured_ehbp_release_is_safe_when_charge_update_would_fail(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = ApiKey(hashed_key="ehbp-rollback-parent", balance=10_000)
    child = ApiKey(
        hashed_key="ehbp-missing-child",
        balance=0,
        parent_key_hash="ehbp-rollback-parent",
    )
    session.add(parent)
    session.add(child)
    await session.commit()
    await pay_for_request(child, 3_000, session)
    reservation = await get_reservation_snapshot(child, session)
    _fail_nth_api_key_update(session, monkeypatch, target_update=2)

    charged = await finalize_ehbp_max_cost_payment(
        child,
        session,
        max_cost_for_model=3_000,
        model_id="tinfoil/model",
        reservation_snapshot=reservation,
    )

    assert charged == 0
    updated_parent = await _api_key(session, "ehbp-rollback-parent")
    assert updated_parent is not None
    assert updated_parent.balance == 10_000
    # The injected partial-update failure rolls aggregate subtraction back;
    # terminal fencing prevents a charge or retry from consuming those funds.
    assert updated_parent.reserved_balance == 3_000
    assert updated_parent.total_spent == 0
    updated_child = await _api_key(session, "ehbp-missing-child")
    assert updated_child is not None
    assert updated_child.reserved_balance == 3_000
    assert updated_child.total_spent == 0
    release = await session.get(ReservationRelease, reservation.release_id)
    assert release is not None and release.status == "released"
    assert reservation.release_id not in auth_module._reservation_heartbeats


@pytest.mark.asyncio
async def test_corrupt_child_aggregate_does_not_erase_parent_sibling_reserve(
    session: AsyncSession,
) -> None:
    parent = ApiKey(hashed_key="ehbp-corrupt-parent", balance=10_000)
    child = ApiKey(
        hashed_key="ehbp-corrupt-child",
        balance=0,
        parent_key_hash=parent.hashed_key,
    )
    session.add(parent)
    session.add(child)
    await session.commit()
    await pay_for_request(child, 3_000, session)
    reservation = await get_reservation_snapshot(child, session)

    await session.exec(  # type: ignore[call-overload]
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == "ehbp-corrupt-parent")
        .values(reserved_balance=5_000)
    )
    await session.exec(  # type: ignore[call-overload]
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == "ehbp-corrupt-child")
        .values(reserved_balance=1_000)
    )
    await session.commit()

    charged = await finalize_ehbp_actual_cost_payment(
        child,
        session,
        reserved_cost_for_model=3_000,
        model_id="tinfoil/model",
        cost_info={"total_msats": 1_200},
        reservation_snapshot=reservation,
    )

    assert charged == 0
    updated_parent = await _api_key(session, "ehbp-corrupt-parent")
    updated_child = await _api_key(session, "ehbp-corrupt-child")
    assert updated_parent is not None and updated_child is not None
    assert updated_parent.balance == 10_000
    assert updated_parent.reserved_balance == 5_000
    assert updated_parent.total_spent == 0
    assert updated_child.reserved_balance == 1_000
    assert updated_child.total_spent == 0
    release = await session.get(ReservationRelease, reservation.release_id)
    assert release is not None and release.status == "released"
    assert reservation.release_id not in auth_module._reservation_heartbeats


def test_zero_debit_ehbp_headers_preserve_computed_cost() -> None:
    headers: dict[str, str] = {}

    _inject_cost_response_headers(
        headers,
        {
            "total_msats": 0,
            "computed_msats": 1_500,
            "input_msats": 1_200,
            "output_msats": 300,
        },
    )

    assert headers["X-Routstr-Cost-Msats"] == "0"
    assert headers["X-Routstr-Computed-Cost-Msats"] == "1500"
