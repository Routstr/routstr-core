"""
Integration tests for the balance-goes-negative bug in adjust_payment_for_tokens.

Root cause: when actual token cost exceeds the discounted reservation
(cost_difference > 0, caused by tolerance_percentage discounting the reservation),
the finalization UPDATE had no WHERE guard on balance, allowing balance to go negative.

Fix: added `.where(col(ApiKey.balance) >= total_cost_msats)` so the UPDATE is a no-op
when balance is insufficient, then falls back to charging only deducted_max_cost.
"""

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


async def _refresh(session: AsyncSession, key: ApiKey) -> ApiKey:
    await session.refresh(key)
    return key


# ---------------------------------------------------------------------------
# Helper: build a CostData where token cost > deducted_max_cost
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Test 1 — exact reproduction of the bug
#
# Setup: balance == deducted_max_cost (user has just enough for the reservation,
#         nothing extra). Actual token cost is 1% higher (tolerance_percentage).
#
# Before fix: balance -= total_cost_msats → goes negative.
# After fix:  WHERE balance >= total_cost_msats fails → fallback charges
#             deducted_max_cost → balance reaches 0, never negative.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_balance_never_negative_when_cost_exceeds_reservation(
    integration_session: AsyncSession,
) -> None:
    """Balance must not go negative when actual token cost > discounted reservation."""
    from routstr.auth import adjust_payment_for_tokens

    deducted_max_cost = 990  # reserved (1% below true max of 1000)
    actual_token_cost = 1000  # actual cost at true max

    # User has balance exactly equal to the reservation — tight budget
    key = _make_key(balance=deducted_max_cost, reserved=deducted_max_cost)
    integration_session.add(key)
    await integration_session.commit()

    response_data = {"model": "test-model", "usage": {"prompt_tokens": 100, "completion_tokens": 100}}

    with patch(
        "routstr.auth.calculate_cost",
        return_value=_cost_data(actual_token_cost),
    ):
        await adjust_payment_for_tokens(key, response_data, integration_session, deducted_max_cost)

    await _refresh(integration_session, key)

    assert key.balance >= 0, f"Balance went negative: {key.balance}"
    assert key.reserved_balance >= 0, f"Reserved balance went negative: {key.reserved_balance}"
    assert key.reserved_balance == 0, "Reservation must be fully released after finalization"


# ---------------------------------------------------------------------------
# Test 2 — balance is ZERO after the reservation is accounted for
#           (absolute floor case)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_balance_floor_at_zero_on_overrun(
    integration_session: AsyncSession,
) -> None:
    """When balance exactly covers deducted_max_cost and cost overruns, balance reaches 0 not negative."""
    from routstr.auth import adjust_payment_for_tokens

    deducted_max_cost = 500
    actual_token_cost = 550  # 10% overrun

    key = _make_key(balance=500, reserved=500)
    integration_session.add(key)
    await integration_session.commit()

    response_data = {"model": "test-model", "usage": {"prompt_tokens": 50, "completion_tokens": 50}}

    with patch(
        "routstr.auth.calculate_cost",
        return_value=_cost_data(actual_token_cost),
    ):
        await adjust_payment_for_tokens(key, response_data, integration_session, deducted_max_cost)

    await _refresh(integration_session, key)

    assert key.balance == 0, (
        f"Expected balance=0 (charged deducted_max_cost fallback), got {key.balance}"
    )
    assert key.reserved_balance == 0, f"Reserved balance should be 0, got {key.reserved_balance}"
    # Fallback charges deducted_max_cost
    assert key.total_spent == deducted_max_cost, (
        f"Expected total_spent={deducted_max_cost}, got {key.total_spent}"
    )


# ---------------------------------------------------------------------------
# Test 3 — balance has enough room: full token cost should be charged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_cost_charged_when_balance_sufficient_for_overrun(
    integration_session: AsyncSession,
) -> None:
    """When balance covers total_cost_msats, the full amount is charged (not just deducted_max_cost)."""
    from routstr.auth import adjust_payment_for_tokens

    deducted_max_cost = 990
    actual_token_cost = 1000

    # User has extra balance beyond the reservation
    key = _make_key(balance=2000, reserved=990)
    integration_session.add(key)
    await integration_session.commit()

    response_data = {"model": "test-model", "usage": {"prompt_tokens": 100, "completion_tokens": 100}}

    with patch(
        "routstr.auth.calculate_cost",
        return_value=_cost_data(actual_token_cost),
    ):
        await adjust_payment_for_tokens(key, response_data, integration_session, deducted_max_cost)

    await _refresh(integration_session, key)

    assert key.balance >= 0, f"Balance went negative: {key.balance}"
    assert key.reserved_balance == 0, f"Reservation not released: {key.reserved_balance}"
    assert key.total_spent == actual_token_cost, (
        f"Expected full charge of {actual_token_cost}, got {key.total_spent}"
    )
    assert key.balance == 2000 - actual_token_cost, (
        f"Expected balance={2000 - actual_token_cost}, got {key.balance}"
    )


# ---------------------------------------------------------------------------
# Test 4 — concurrent finalizations with cost overrun
#
# Multiple requests finish concurrently. Each has a small overrun.
# None should drive balance negative.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_cost_overruns_never_negative(
    integration_session: AsyncSession,
) -> None:
    """Concurrent finalization with cost overruns must never produce negative balance."""
    import asyncio

    from routstr.auth import adjust_payment_for_tokens, pay_for_request
    from routstr.core.db import create_session

    deducted_max_cost = 990
    actual_token_cost = 1000
    n_requests = 5

    # Fund the key with exactly enough for n_requests reservations + a tiny buffer
    starting_balance = deducted_max_cost * n_requests
    key_hash = f"test_concurrent_{uuid.uuid4().hex}"

    async with create_session() as session:
        key = ApiKey(
            hashed_key=key_hash,
            balance=starting_balance,
            reserved_balance=0,
            total_spent=0,
            total_requests=0,
        )
        session.add(key)
        await session.commit()

    # Reserve n_requests slots (sequentially, as pay_for_request is atomic)
    async with create_session() as session:
        key_to_reserve = await session.get(ApiKey, key_hash)
        assert key_to_reserve is not None
        for _ in range(n_requests):
            await pay_for_request(key_to_reserve, deducted_max_cost, session)
            await session.refresh(key_to_reserve)

    # Now finalize all concurrently with cost overrun
    async def finalize() -> None:
        response_data = {
            "model": "test-model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},
        }
        async with create_session() as session:
            fresh_key = await session.get(ApiKey, key_hash)
            assert fresh_key is not None
            with patch(
                "routstr.auth.calculate_cost",
                return_value=_cost_data(actual_token_cost),
            ):
                await adjust_payment_for_tokens(
                    fresh_key, response_data, session, deducted_max_cost
                )

    await asyncio.gather(*[finalize() for _ in range(n_requests)])

    async with create_session() as session:
        final_key = await session.get(ApiKey, key_hash)
        assert final_key is not None

    assert final_key.balance >= 0, (
        f"Balance went negative after concurrent overruns: {final_key.balance}"
    )
    assert final_key.reserved_balance == 0, (
        f"Reserved balance not fully released: {final_key.reserved_balance}"
    )
    assert final_key.total_spent <= starting_balance, (
        f"Total spent ({final_key.total_spent}) exceeds starting balance ({starting_balance})"
    )
    # Every request must have been charged at least deducted_max_cost — no free inference.
    assert final_key.total_spent == starting_balance, (
        f"Expected total_spent={starting_balance} (all {n_requests} reservations charged), "
        f"got {final_key.total_spent} — at least one request got free inference"
    )


# ---------------------------------------------------------------------------
# Test 5 — overrun with no balance at all (reserved_balance == balance)
#           simulates a user who topped up to exactly the reservation floor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_free_balance_overrun_is_safe(
    integration_session: AsyncSession,
) -> None:
    """User with zero free balance (all reserved) should never go negative on overrun."""
    from routstr.auth import adjust_payment_for_tokens

    deducted_max_cost = 1000
    actual_token_cost = 1050

    # balance == reserved_balance: zero free balance
    key = _make_key(balance=1000, reserved=1000)
    integration_session.add(key)
    await integration_session.commit()

    response_data = {"model": "test-model", "usage": {"prompt_tokens": 50, "completion_tokens": 100}}

    with patch(
        "routstr.auth.calculate_cost",
        return_value=_cost_data(actual_token_cost),
    ):
        await adjust_payment_for_tokens(key, response_data, integration_session, deducted_max_cost)

    await _refresh(integration_session, key)

    assert key.balance >= 0, f"Balance went negative: {key.balance}"
    assert key.reserved_balance >= 0, f"Reserved balance went negative: {key.reserved_balance}"


# ---------------------------------------------------------------------------
# Test 6 — parallel requests: second finalization must not get free inference
#
# Root cause of the bug fixed in auth.py:
#   `.where(col(ApiKey.balance) >= total_cost_msats)` ignores other requests'
#   reservations, so after Request A charges total_cost_msats, balance can drop
#   below deducted_max_cost, causing Request B's fallback to release for free.
#
# Fix: use `balance - reserved_balance + deducted_max_cost >= total_cost_msats`
#   so the check accounts for concurrent reservations.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_requests_no_free_inference(
    integration_session: AsyncSession,
) -> None:
    """Second parallel finalization must be charged even when first depleted free balance."""
    import asyncio

    from routstr.auth import adjust_payment_for_tokens
    from routstr.core.db import create_session

    deducted_max_cost = 100
    actual_token_cost = 150  # overrun: 50 more than reserved

    # Fund the key with exactly 2 * deducted_max_cost.
    # Both requests pre-reserved 100 each → balance=200, reserved=200, free=0.
    # Old check (balance >= total_cost_msats):
    #   Request A: 200 >= 150 ✓ → charges 150 → balance=50, reserved=100
    #   Request B: 50 >= 150 ✗ → fallback: 50 >= 100 ✗ → releases FREE
    # New check (balance - reserved + deducted >= total_cost_msats):
    #   Both fall to fallback (0 free balance).
    #   Both charge deducted_max_cost=100 → total_spent=200, balance=0.
    starting_balance = deducted_max_cost * 2
    key_hash = f"test_parallel_no_free_{uuid.uuid4().hex}"

    async with create_session() as session:
        key = ApiKey(
            hashed_key=key_hash,
            balance=starting_balance,
            reserved_balance=deducted_max_cost * 2,  # both slots pre-reserved
            total_spent=0,
            total_requests=2,
        )
        session.add(key)
        await session.commit()

    async def finalize() -> None:
        response_data = {
            "model": "test-model",
            "usage": {"prompt_tokens": 50, "completion_tokens": 100},
        }
        async with create_session() as session:
            fresh_key = await session.get(ApiKey, key_hash)
            assert fresh_key is not None
            with patch(
                "routstr.auth.calculate_cost",
                return_value=_cost_data(actual_token_cost),
            ):
                await adjust_payment_for_tokens(
                    fresh_key, response_data, session, deducted_max_cost
                )

    await asyncio.gather(finalize(), finalize())

    async with create_session() as session:
        final_key = await session.get(ApiKey, key_hash)
        assert final_key is not None

    assert final_key.balance >= 0, f"Balance went negative: {final_key.balance}"
    assert final_key.reserved_balance == 0, (
        f"Reserved balance not released: {final_key.reserved_balance}"
    )
    # Both requests must have been charged — no free inference.
    assert final_key.total_spent == starting_balance, (
        f"Expected total_spent={starting_balance} (both reservations charged), "
        f"got {final_key.total_spent} — one request got free inference"
    )
