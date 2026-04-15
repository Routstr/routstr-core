"""
Tests for the reservation lifecycle:

  1. Reserve  → reserved_balance increases, available (total_balance) decreases.
  2. Reserve  → revert  → reserved_balance restored, balance untouched.
  3. Reserve  → finalise → reserved_balance released, balance charged.
  4. Two parallel reserves, only one fits → second blocked with 402.
  5. Three parallel reserves, two fit, third blocked with 402.
  6. Sequential reserves until balance exhausted → next request blocked.

Reservation invariant enforced by the atomic WHERE clause in pay_for_request:
    balance - reserved_balance >= cost_per_request
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import pay_for_request, revert_pay_for_request
from routstr.core.db import ApiKey, create_session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_key(balance: int, reserved: int = 0) -> ApiKey:
    return ApiKey(
        hashed_key=f"test_{uuid.uuid4().hex}",
        balance=balance,
        reserved_balance=reserved,
        total_spent=0,
        total_requests=0,
    )


async def _persist(session: AsyncSession, key: ApiKey) -> ApiKey:
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key


# ---------------------------------------------------------------------------
# Test 1 — Reserve: reserved_balance increases, available balance decreases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reserve_increases_reserved_balance(
    integration_session: AsyncSession,
) -> None:
    """pay_for_request must increment reserved_balance by cost_per_request."""
    cost = 100
    key = await _persist(integration_session, _make_key(balance=500))

    await pay_for_request(key, cost, integration_session)
    await integration_session.refresh(key)

    assert key.reserved_balance == cost
    assert key.balance == 500  # balance column is NOT decremented on reserve
    assert key.total_balance == 500 - cost  # available = balance - reserved


# ---------------------------------------------------------------------------
# Test 2 — Revert: reserved_balance restored, balance untouched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revert_releases_reservation(
    integration_session: AsyncSession,
) -> None:
    """revert_pay_for_request must release the reservation without touching balance."""
    cost = 150
    key = await _persist(integration_session, _make_key(balance=300))

    await pay_for_request(key, cost, integration_session)
    await integration_session.refresh(key)
    assert key.reserved_balance == cost

    await revert_pay_for_request(key, integration_session, cost)
    await integration_session.refresh(key)

    assert key.reserved_balance == 0
    assert key.balance == 300  # balance unchanged after revert


# ---------------------------------------------------------------------------
# Test 3 — Finalise: reservation released + balance charged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finalise_releases_reservation_and_charges_balance(
    integration_session: AsyncSession,
) -> None:
    """adjust_payment_for_tokens must zero reserved_balance and deduct actual cost."""
    from unittest.mock import patch

    from routstr.auth import adjust_payment_for_tokens
    from routstr.payment.cost_calculation import CostData

    cost = 100
    actual = 80  # actual < reserved → refund path
    key = await _persist(integration_session, _make_key(balance=500))

    await pay_for_request(key, cost, integration_session)
    await integration_session.refresh(key)
    assert key.reserved_balance == cost

    cost_data = CostData(
        base_msats=0,
        input_msats=40,
        output_msats=40,
        total_msats=actual,
        total_usd=0.0,
        input_tokens=50,
        output_tokens=50,
    )
    response_data = {"model": "test-model", "usage": {"prompt_tokens": 50, "completion_tokens": 50}}

    with patch("routstr.auth.calculate_cost", return_value=cost_data):
        await adjust_payment_for_tokens(key, response_data, integration_session, cost)

    await integration_session.refresh(key)

    assert key.reserved_balance == 0
    assert key.balance == 500 - actual
    assert key.total_spent == actual


# ---------------------------------------------------------------------------
# Test 4 — Concurrent: second parallel reserve blocked when balance exhausted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_second_reserve_blocked_when_balance_exhausted(
    patched_db_engine: None,
) -> None:
    """When two requests race for the same balance, only one succeeds; the other gets 402."""
    cost = 300
    key_hash = f"test_concurrent_{uuid.uuid4().hex}"

    async with create_session() as session:
        key = ApiKey(
            hashed_key=key_hash,
            balance=300,       # exactly enough for ONE reservation
            reserved_balance=0,
            total_spent=0,
            total_requests=0,
        )
        session.add(key)
        await session.commit()

    results: list[str] = []

    async def attempt_reserve() -> None:
        async with create_session() as session:
            fresh_key = await session.get(ApiKey, key_hash)
            assert fresh_key is not None
            try:
                await pay_for_request(fresh_key, cost, session)
                results.append("success")
            except HTTPException as exc:
                assert exc.status_code == 402
                results.append("blocked")

    await asyncio.gather(attempt_reserve(), attempt_reserve())

    assert sorted(results) == ["blocked", "success"], (
        f"Expected exactly one success and one 402, got: {results}"
    )

    async with create_session() as session:
        final = await session.get(ApiKey, key_hash)
        assert final is not None

    # reserved_balance must equal exactly one reservation (not two)
    assert final.reserved_balance == cost, (
        f"Expected reserved_balance={cost}, got {final.reserved_balance}"
    )
    assert final.balance == 300, "Balance column must not be modified by reservation"


# ---------------------------------------------------------------------------
# Test 5 — Concurrent: three requests, two fit, third blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_three_parallel_reserves_third_blocked(
    patched_db_engine: None,
) -> None:
    """Balance covers two reservations exactly; the third concurrent request must be blocked."""
    cost = 100
    key_hash = f"test_three_parallel_{uuid.uuid4().hex}"

    async with create_session() as session:
        key = ApiKey(
            hashed_key=key_hash,
            balance=200,       # fits exactly 2 reservations of 100
            reserved_balance=0,
            total_spent=0,
            total_requests=0,
        )
        session.add(key)
        await session.commit()

    results: list[str] = []

    async def attempt_reserve() -> None:
        async with create_session() as session:
            fresh_key = await session.get(ApiKey, key_hash)
            assert fresh_key is not None
            try:
                await pay_for_request(fresh_key, cost, session)
                results.append("success")
            except HTTPException as exc:
                assert exc.status_code == 402
                results.append("blocked")

    await asyncio.gather(
        attempt_reserve(),
        attempt_reserve(),
        attempt_reserve(),
    )

    successes = results.count("success")
    blocked = results.count("blocked")

    assert successes == 2, f"Expected 2 successes, got {successes}: {results}"
    assert blocked == 1, f"Expected 1 blocked, got {blocked}: {results}"

    async with create_session() as session:
        final = await session.get(ApiKey, key_hash)
        assert final is not None

    assert final.reserved_balance == cost * 2, (
        f"Expected reserved_balance={cost * 2}, got {final.reserved_balance}"
    )
    assert final.balance == 200, "Balance column must not be modified by reservation"


# ---------------------------------------------------------------------------
# Test 6 — Sequential exhaustion: reserve until empty, next request blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sequential_reserves_block_when_balance_exhausted(
    integration_session: AsyncSession,
) -> None:
    """Repeated reservations should block as soon as available balance drops below cost."""
    cost = 100
    key = await _persist(integration_session, _make_key(balance=250))

    # First two succeed (100 + 100 = 200 ≤ 250)
    await pay_for_request(key, cost, integration_session)
    await pay_for_request(key, cost, integration_session)
    await integration_session.refresh(key)
    assert key.reserved_balance == 200
    assert key.total_balance == 50  # 250 - 200

    # Third: only 50 available, need 100 → blocked
    with pytest.raises(HTTPException) as exc_info:
        await pay_for_request(key, cost, integration_session)

    assert exc_info.value.status_code == 402
    await integration_session.refresh(key)
    assert key.reserved_balance == 200  # unchanged after failed reserve
