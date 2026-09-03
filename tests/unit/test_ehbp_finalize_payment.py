from __future__ import annotations

from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
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
async def test_unmeasured_ehbp_releases_reservation(
    session: AsyncSession,
) -> None:
    key = ApiKey(hashed_key="ehbp-key", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)

    charged = await finalize_ehbp_max_cost_payment(
        key,
        session,
        max_cost_for_model=3_000,
        model_id="tinfoil/model",
        reservation_snapshot=reservation,
    )

    assert charged == 0
    updated = await _api_key(session, "ehbp-key")
    assert updated is not None
    assert updated.balance == 10_000
    assert updated.reserved_balance == 0
    assert updated.reserved_at is None
    assert updated.total_spent == 0


@pytest.mark.asyncio
async def test_finalize_actual_cost_payment_rolls_back_when_billing_key_update_matches_no_rows(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ApiKey(hashed_key="ehbp-failed-billing-update", balance=10_000)
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
    updated = await _api_key(session, "ehbp-failed-billing-update")
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
    key = ApiKey(hashed_key="ehbp-rollback-key", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)
    _fail_nth_api_key_update(session, monkeypatch, target_update=1)

    charged = await finalize_ehbp_max_cost_payment(
        key,
        session,
        max_cost_for_model=3_000,
        model_id="tinfoil/model",
        reservation_snapshot=reservation,
    )

    assert charged == 0
    updated = await _api_key(session, "ehbp-rollback-key")
    assert updated is not None
    assert updated.balance == 10_000
    # The injected partial-update failure rolls aggregate subtraction back;
    # terminal fencing prevents a charge or retry from consuming those funds.
    assert updated.reserved_balance == 3_000
    assert updated.total_spent == 0
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
