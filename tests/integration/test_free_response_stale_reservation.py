"""Regression tests for charging after stale reservation cleanup."""

import time
import uuid
from unittest.mock import patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey
from routstr.payment.cost_calculation import CostData


def _make_key(balance: int, reserved: int) -> ApiKey:
    return ApiKey(
        hashed_key=f"test_{uuid.uuid4().hex}",
        balance=balance,
        reserved_balance=reserved,
        total_spent=0,
        total_requests=1,
    )


def _cost_data(total_msats: int) -> CostData:
    return CostData(
        base_msats=0,
        input_msats=total_msats // 2,
        output_msats=total_msats - total_msats // 2,
        total_msats=total_msats,
        total_usd=0.0,
        input_tokens=100,
        output_tokens=100,
    )


@pytest.mark.asyncio
async def test_overrun_charges_after_reservation_swept(
    integration_session: AsyncSession,
) -> None:
    """Overrun finalize must charge even when the reservation was already released."""
    from routstr.auth import adjust_payment_for_tokens, pay_for_request

    deducted_max_cost = 990  # discounted reservation
    actual_token_cost = 1000  # actual cost overruns the reservation

    # Sweeper has zeroed reserved_balance but left balance untouched.
    key = _make_key(balance=1000, reserved=0)
    integration_session.add(key)
    await integration_session.commit()
    await pay_for_request(key, deducted_max_cost, integration_session)
    key.reserved_balance = 0
    integration_session.add(key)
    await integration_session.commit()

    response_data = {
        "model": "test-model",
        "usage": {"prompt_tokens": 100, "completion_tokens": 100},
    }

    with patch(
        "routstr.auth.calculate_cost",
        return_value=_cost_data(actual_token_cost),
    ):
        await adjust_payment_for_tokens(
            key, response_data, integration_session, deducted_max_cost, None, None
        )

    await integration_session.refresh(key)

    assert key.total_spent == actual_token_cost, (
        f"Request was not billed (total_spent={key.total_spent}) — free response bug"
    )
    assert key.balance == 1000 - actual_token_cost, (
        f"Balance not charged: {key.balance}"
    )
    assert key.balance >= 0
    assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_free_response_path_closed_end_to_end(
    integration_session: AsyncSession,
    patched_db_engine: None,
) -> None:
    """A reservation released by the real sweeper must not yield a free response."""
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )
    from routstr.core.db import (
        ReservationRelease,
        create_session,
        release_stale_reservations,
    )

    deducted_max_cost = 990
    actual_token_cost = 1000
    key_hash = f"test_sweep_{uuid.uuid4().hex}"

    async with create_session() as session:
        session.add(
            ApiKey(
                hashed_key=key_hash,
                balance=1000,
                reserved_balance=0,
                total_spent=0,
                total_requests=0,
            )
        )
        await session.commit()

    # Reserve the request, then backdate reserved_at so the sweeper treats it as
    # stale (simulates a stream that outlived stale_reservation_timeout_seconds).
    async with create_session() as session:
        key = await session.get(ApiKey, key_hash)
        assert key is not None
        await pay_for_request(key, deducted_max_cost, session)
        snapshot = await get_reservation_snapshot(key, session)
        await session.refresh(key)
        assert key.reserved_balance == deducted_max_cost
        key.reserved_at = int(time.time()) - 10_000
        record = await session.get(ReservationRelease, snapshot.release_id)
        assert record is not None
        record.created_at = int(time.time()) - 10_000
        session.add(key)
        session.add(record)
        await session.commit()

    # Sweeper releases the stale reservation without charging.
    async with create_session() as session:
        released = await release_stale_reservations(session, max_age_seconds=300)
        assert released == 1

    async with create_session() as session:
        key = await session.get(ApiKey, key_hash)
        assert key is not None
        assert key.reserved_balance == 0, "Precondition: sweeper zeroed the reservation"

        response_data = {
            "model": "test-model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},
        }
        with patch(
            "routstr.auth.calculate_cost",
            return_value=_cost_data(actual_token_cost),
        ):
            await adjust_payment_for_tokens(
                key,
                response_data,
                session,
                deducted_max_cost,
                reservation_snapshot=snapshot,
            )

    async with create_session() as session:
        final = await session.get(ApiKey, key_hash)
        assert final is not None

    # Stale release is terminal for this reservation. A late finalizer must not
    # charge aggregate balance that may now belong to a newer request.
    assert final.total_spent == 0
    assert final.balance == 1000
    assert final.balance >= 0
    assert final.reserved_balance == 0
