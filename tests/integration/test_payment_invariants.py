"""Invariant coverage for the reserve → charge → release money path.

Every finalization branch of ``adjust_payment_for_tokens`` must respect the
same accounting rules: a completed request is charged exactly once, its
reported ``charged_msats`` matches the actual debit, it never spends more than
its own reservation leaves available, and child keys spend their parent's
balance without raiding sibling reservations.
"""

import asyncio
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlmodel import col, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey, ReservationRelease
from routstr.payment.cost_calculation import (
    CostData,
    CostDataError,
    MaxCostData,
)

pytestmark = pytest.mark.integration


def _cost_data(total_msats: int, cls: type[CostData] = CostData) -> CostData:
    return cls(
        base_msats=0,
        input_msats=total_msats // 2,
        output_msats=total_msats - total_msats // 2,
        total_msats=total_msats,
        total_usd=0.0,
        input_tokens=100,
        output_tokens=100,
    )


def _response() -> dict:
    return {
        "model": "test-model",
        "usage": {"prompt_tokens": 100, "completion_tokens": 100},
    }


async def _new_key(
    session: AsyncSession,
    balance: int,
    *,
    parent_key_hash: str | None = None,
    balance_limit: int | None = None,
) -> str:
    key_hash = f"test_inv_{uuid.uuid4().hex}"
    session.add(
        ApiKey(
            hashed_key=key_hash,
            balance=balance,
            reserved_balance=0,
            total_spent=0,
            total_requests=0,
            parent_key_hash=parent_key_hash,
            balance_limit=balance_limit,
        )
    )
    await session.commit()
    return key_hash


async def _active_reservations(session: AsyncSession) -> int:
    rows = await session.exec(
        select(ReservationRelease).where(col(ReservationRelease.status) == "active")
    )
    return len(rows.all())


@pytest.mark.asyncio
async def test_corrupt_revert_still_decrements_request_count(
    integration_session: AsyncSession,
) -> None:
    from routstr.auth import (
        get_reservation_snapshot,
        pay_for_request,
        revert_pay_for_request,
    )

    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None
    await pay_for_request(key, 3_000, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    await integration_session.exec(  # type: ignore[call-overload]
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key_hash)
        .values(reserved_balance=0)
    )
    await integration_session.commit()

    assert await revert_pay_for_request(
        key,
        integration_session,
        3_000,
        reservation_snapshot=reservation,
    )
    updated = await integration_session.get(ApiKey, key_hash)
    record = await integration_session.get(ReservationRelease, reservation.release_id)
    assert updated is not None
    assert updated.total_requests == 0
    assert updated.reserved_balance == 0
    assert record is not None and record.status == "released"


@pytest.mark.asyncio
async def test_exact_cost_branch_charges_the_reservation_once(
    integration_session: AsyncSession,
) -> None:
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    cost = 3_000
    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, cost, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    with patch("routstr.auth.calculate_cost", return_value=_cost_data(cost)):
        result = await adjust_payment_for_tokens(
            key,
            _response(),
            integration_session,
            cost,
            reservation_snapshot=reservation,
        )

    assert result["charged_msats"] == cost
    await integration_session.refresh(key)
    assert key.balance == 10_000 - cost
    assert key.total_spent == cost
    assert key.reserved_balance == 0
    assert key.total_requests == 1
    assert await _active_reservations(integration_session) == 0


@pytest.mark.asyncio
async def test_max_cost_branch_charges_the_reservation_once(
    integration_session: AsyncSession,
) -> None:
    """No token pricing configured -> flat MaxCostData charge of the reservation."""
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    cost = 2_500
    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, cost, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    with patch(
        "routstr.auth.calculate_cost",
        return_value=_cost_data(cost, cls=MaxCostData),
    ):
        result = await adjust_payment_for_tokens(
            key,
            _response(),
            integration_session,
            cost,
            reservation_snapshot=reservation,
        )

    assert result["charged_msats"] == cost
    await integration_session.refresh(key)
    assert key.balance == 10_000 - cost
    assert key.total_spent == cost
    assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_underrun_branch_refunds_the_unused_reservation(
    integration_session: AsyncSession,
) -> None:
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    reserved = 5_000
    actual = 1_200
    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, reserved, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    with patch("routstr.auth.calculate_cost", return_value=_cost_data(actual)):
        result = await adjust_payment_for_tokens(
            key,
            _response(),
            integration_session,
            reserved,
            reservation_snapshot=reservation,
        )

    assert result["charged_msats"] == actual
    await integration_session.refresh(key)
    assert key.total_spent == actual, "user must pay the real cost, not the reservation"
    assert key.balance == 10_000 - actual
    assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_overrun_branch_charges_full_cost_when_balance_is_free(
    integration_session: AsyncSession,
) -> None:
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    reserved = 1_000
    actual = 1_400
    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, reserved, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    with patch("routstr.auth.calculate_cost", return_value=_cost_data(actual)):
        result = await adjust_payment_for_tokens(
            key,
            _response(),
            integration_session,
            reserved,
            reservation_snapshot=reservation,
        )

    assert result["charged_msats"] == actual
    await integration_session.refresh(key)
    assert key.total_spent == actual
    assert key.balance == 10_000 - actual
    assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_zero_cost_response_is_free_and_releases_the_reservation(
    integration_session: AsyncSession,
) -> None:
    """An empty/unusable upstream response must cost the user nothing."""
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    reserved = 4_000
    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, reserved, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    with patch("routstr.auth.calculate_cost", return_value=_cost_data(0)):
        await adjust_payment_for_tokens(
            key,
            {"model": "test-model"},
            integration_session,
            reserved,
            reservation_snapshot=reservation,
        )

    await integration_session.refresh(key)
    assert key.balance == 10_000
    assert key.total_spent == 0
    assert key.reserved_balance == 0
    assert await _active_reservations(integration_session) == 0


@pytest.mark.asyncio
async def test_cost_error_releases_the_reservation_without_charging(
    integration_session: AsyncSession,
) -> None:
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    reserved = 4_000
    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, reserved, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    with patch(
        "routstr.auth.calculate_cost",
        return_value=CostDataError(message="no pricing", code="pricing_error"),
    ):
        with pytest.raises(HTTPException) as exc:
            await adjust_payment_for_tokens(
                key,
                _response(),
                integration_session,
                reserved,
                reservation_snapshot=reservation,
            )
    assert exc.value.status_code == 400

    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None
    assert key.balance == 10_000, "a pricing failure must not charge the user"
    assert key.total_spent == 0
    assert key.reserved_balance == 0, "funds must not stay locked after a 400"
    assert await _active_reservations(integration_session) == 0


@pytest.mark.asyncio
async def test_repeated_finalization_charges_only_once(
    integration_session: AsyncSession,
) -> None:
    """A retried finalizer (proxy retry, duplicate stream end) must not double-bill."""
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    cost = 3_000
    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, cost, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    results = []
    with patch("routstr.auth.calculate_cost", return_value=_cost_data(cost)):
        for _ in range(3):
            # A declined re-charge rolls its session back, so re-load the key
            # the way a fresh request would instead of reusing a stale instance.
            integration_session.expunge_all()
            key = await integration_session.get(ApiKey, key_hash)
            assert key is not None
            results.append(
                await adjust_payment_for_tokens(
                    key,
                    _response(),
                    integration_session,
                    cost,
                    reservation_snapshot=reservation,
                )
            )

    # Only the first finalization debits; duplicates report a zero charge.
    assert [r["charged_msats"] for r in results] == [cost, 0, 0]

    integration_session.expunge_all()
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None
    assert key.total_spent == cost, f"charged {key.total_spent} for one request"
    assert key.balance == 10_000 - cost


@pytest.mark.asyncio
async def test_concurrent_duplicate_finalization_charges_only_once(
    integration_session: AsyncSession,
    patched_db_engine: None,
) -> None:
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )
    from routstr.core.db import create_session

    cost = 3_000
    async with create_session() as session:
        key_hash = await _new_key(session, balance=10_000)
        key = await session.get(ApiKey, key_hash)
        assert key is not None
        await pay_for_request(key, cost, session)
        reservation = await get_reservation_snapshot(key, session)

    async def finalize() -> None:
        async with create_session() as session:
            fresh = await session.get(ApiKey, key_hash)
            assert fresh is not None
            await adjust_payment_for_tokens(
                fresh,
                _response(),
                session,
                cost,
                reservation_snapshot=reservation,
            )

    with patch("routstr.auth.calculate_cost", return_value=_cost_data(cost)):
        await asyncio.gather(finalize(), finalize(), finalize())

    async with create_session() as session:
        key = await session.get(ApiKey, key_hash)
    assert key is not None
    assert key.total_spent == cost
    assert key.balance == 10_000 - cost
    assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_release_after_charge_does_not_credit_the_user_back(
    integration_session: AsyncSession,
) -> None:
    """A late cleanup path must not turn a charged request into a free one."""
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
        release_reservation,
    )

    cost = 3_000
    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, cost, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    with patch("routstr.auth.calculate_cost", return_value=_cost_data(cost)):
        await adjust_payment_for_tokens(
            key,
            _response(),
            integration_session,
            cost,
            reservation_snapshot=reservation,
        )

    released = await release_reservation(reservation, integration_session, cost)
    assert released is False, "a charged reservation must not be releasable"

    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None
    assert key.balance == 10_000 - cost
    assert key.total_spent == cost
    assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_child_request_spends_parent_balance_and_records_child_ledger(
    integration_session: AsyncSession,
) -> None:
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    cost = 3_000
    parent_hash = await _new_key(integration_session, balance=10_000)
    child_hash = await _new_key(
        integration_session, balance=0, parent_key_hash=parent_hash
    )
    child = await integration_session.get(ApiKey, child_hash)
    assert child is not None

    await pay_for_request(child, cost, integration_session)
    reservation = await get_reservation_snapshot(child, integration_session)

    with patch("routstr.auth.calculate_cost", return_value=_cost_data(cost)):
        await adjust_payment_for_tokens(
            child,
            _response(),
            integration_session,
            cost,
            reservation_snapshot=reservation,
        )

    parent = await integration_session.get(ApiKey, parent_hash)
    child = await integration_session.get(ApiKey, child_hash)
    assert parent is not None and child is not None
    assert parent.balance == 10_000 - cost
    assert parent.reserved_balance == 0
    assert parent.total_balance >= 0
    assert child.reserved_balance == 0, "child reservation must be released too"
    assert child.total_balance >= 0
    assert child.total_spent == cost, "child ledger must record the spend"
    assert parent.total_spent == cost


@pytest.mark.asyncio
async def test_child_overrun_does_not_raid_a_sibling_reservation(
    integration_session: AsyncSession,
) -> None:
    """Same overrun defect as the parent case, reached through a child key."""
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    reserved_each = 100
    overrun = 150
    parent_hash = await _new_key(integration_session, balance=2 * reserved_each)
    child_a = await _new_key(
        integration_session, balance=0, parent_key_hash=parent_hash
    )
    child_b = await _new_key(
        integration_session, balance=0, parent_key_hash=parent_hash
    )

    key_a = await integration_session.get(ApiKey, child_a)
    key_b = await integration_session.get(ApiKey, child_b)
    assert key_a is not None and key_b is not None

    await pay_for_request(key_a, reserved_each, integration_session)
    reservation_a = await get_reservation_snapshot(key_a, integration_session)
    await pay_for_request(key_b, reserved_each, integration_session)
    await get_reservation_snapshot(key_b, integration_session)

    with patch("routstr.auth.calculate_cost", return_value=_cost_data(overrun)):
        await adjust_payment_for_tokens(
            key_a,
            _response(),
            integration_session,
            reserved_each,
            reservation_snapshot=reservation_a,
        )

    parent = await integration_session.get(ApiKey, parent_hash)
    assert parent is not None
    assert parent.total_balance >= 0, (
        f"child A's overrun ate child B's reservation: balance={parent.balance} "
        f"reserved={parent.reserved_balance}"
    )
