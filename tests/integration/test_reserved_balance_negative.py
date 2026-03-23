"""Test to verify reserved balance never goes negative."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey, create_session


@pytest.mark.asyncio
async def test_reserved_balance_never_negative(integration_client: AsyncClient) -> None:
    """Test that reserved balance never goes negative under various conditions."""

    # Create a test API key with limited balance
    async with create_session() as session:
        test_key = ApiKey(
            hashed_key="test_reserved_balance_key",
            balance=1000,  # 1 sat
            reserved_balance=0,
        )
        session.add(test_key)
        await session.commit()

    bearer_token = "sk-test_reserved_balance_key"
    headers = {"Authorization": f"Bearer {bearer_token}"}

    # Test 1: Make a request that will fail upstream
    # This should reserve funds and then revert them
    await integration_client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "invalid-model-that-will-fail",
            "messages": [{"role": "user", "content": "test"}],
        },
    )

    # Check reserved balance after failed request
    async with create_session() as session:
        key = await session.get(ApiKey, "test_reserved_balance_key")
        assert key is not None
        assert key.reserved_balance >= 0, (
            f"Reserved balance went negative: {key.reserved_balance}"
        )
        assert key.balance == 1000, (
            "Balance should remain unchanged after failed request"
        )

    # Test 2: Simulate concurrent failed requests
    # This tests the race condition protection
    async def make_failing_request() -> None:
        try:
            await integration_client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "invalid-model",
                    "messages": [{"role": "user", "content": "test"}],
                },
            )
        except Exception:
            pass  # Expected to fail

    # Run multiple concurrent requests
    await asyncio.gather(*[make_failing_request() for _ in range(5)])

    # Check final state
    async with create_session() as session:
        key = await session.get(ApiKey, "test_reserved_balance_key")
        assert key is not None
        assert key.reserved_balance >= 0, (
            f"Reserved balance went negative after concurrent requests: {key.reserved_balance}"
        )
        print(f"Final state - Balance: {key.balance}, Reserved: {key.reserved_balance}")


@pytest.mark.asyncio
async def test_reserved_balance_with_successful_requests(
    integration_client: AsyncClient,
) -> None:
    """Test reserved balance handling with successful requests."""

    # Create a test API key with more balance
    async with create_session() as session:
        unique_key = f"test_successful_key_{uuid.uuid4().hex[:8]}"
        test_key = ApiKey(
            hashed_key=unique_key,
            balance=100000,  # 100 sats
            reserved_balance=0,
        )
        session.add(test_key)
        await session.commit()

    bearer_token = f"sk-{unique_key}"
    headers = {"Authorization": f"Bearer {bearer_token}"}

    # Make a valid request (assuming you have a mock or test endpoint)
    # This test might need adjustment based on your test setup
    await integration_client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "gpt-4o-mini",  # Or whatever model is available in test
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 10,
        },
    )

    # Check that reserved balance was properly adjusted
    async with create_session() as session:
        key = await session.get(ApiKey, unique_key)
        assert key is not None
        assert key.reserved_balance >= 0, (
            f"Reserved balance went negative: {key.reserved_balance}"
        )
        # Check if the request was processed (might fail due to model pricing in test env)
        # The important part is that reserved_balance doesn't go negative
        if key.total_spent > 0:
            assert key.balance < 100000, (
                "Balance should decrease after successful request"
            )
        else:
            # Request failed, but reserved balance should still be non-negative
            assert key.balance == 100000, (
                "Balance should remain unchanged if request failed"
            )
        print(
            f"After successful request - Balance: {key.balance}, Reserved: {key.reserved_balance}, Spent: {key.total_spent}"
        )


@pytest.mark.asyncio
async def test_revert_with_zero_reserved_balance_is_noop(
    integration_session: AsyncSession,
) -> None:
    """Test that revert_pay_for_request is a no-op when reserved_balance is 0.

    Previously this would drive reserved_balance negative. With the floor guard,
    it should return False and leave reserved_balance at 0.
    """
    from routstr.auth import revert_pay_for_request

    unique_key = f"test_revert_key_{uuid.uuid4().hex[:8]}"
    test_key = ApiKey(
        hashed_key=unique_key,
        balance=1000,
        reserved_balance=0,
    )
    integration_session.add(test_key)
    await integration_session.commit()

    # Try to revert more than available — should be a no-op
    result = await revert_pay_for_request(test_key, integration_session, 100)

    await integration_session.refresh(test_key)

    assert result is False, "Revert should return False when reservation already released"
    assert test_key.reserved_balance == 0, (
        f"Reserved balance should remain 0, got: {test_key.reserved_balance}"
    )
    assert test_key.total_requests == 0, (
        f"Total requests should remain 0, got: {test_key.total_requests}"
    )


@pytest.mark.asyncio
async def test_revert_with_sufficient_reserved_balance_succeeds(
    integration_session: AsyncSession,
) -> None:
    """Test that revert_pay_for_request works correctly when there is enough reserved balance."""
    from routstr.auth import revert_pay_for_request

    unique_key = f"test_revert_ok_{uuid.uuid4().hex[:8]}"
    test_key = ApiKey(
        hashed_key=unique_key,
        balance=5000,
        reserved_balance=500,
        total_requests=3,
    )
    integration_session.add(test_key)
    await integration_session.commit()

    result = await revert_pay_for_request(test_key, integration_session, 500)

    await integration_session.refresh(test_key)

    assert result is True, "Revert should return True on success"
    assert test_key.reserved_balance == 0, (
        f"Reserved balance should be 0, got: {test_key.reserved_balance}"
    )
    assert test_key.total_requests == 2, (
        f"Total requests should be 2, got: {test_key.total_requests}"
    )
    assert test_key.balance == 5000, "Balance should not change on revert"


@pytest.mark.asyncio
async def test_revert_partial_reserved_balance_is_noop(
    integration_session: AsyncSession,
) -> None:
    """Test that reverting more than the current reserved_balance is a no-op."""
    from routstr.auth import revert_pay_for_request

    unique_key = f"test_revert_partial_{uuid.uuid4().hex[:8]}"
    test_key = ApiKey(
        hashed_key=unique_key,
        balance=5000,
        reserved_balance=50,
        total_requests=1,
    )
    integration_session.add(test_key)
    await integration_session.commit()

    # Try to revert 500 when only 50 is reserved — should be no-op
    result = await revert_pay_for_request(test_key, integration_session, 500)

    await integration_session.refresh(test_key)

    assert result is False, "Revert should fail when cost > reserved_balance"
    assert test_key.reserved_balance == 50, (
        f"Reserved balance should stay at 50, got: {test_key.reserved_balance}"
    )
    assert test_key.total_requests == 1, (
        f"Total requests should stay at 1, got: {test_key.total_requests}"
    )


@pytest.mark.asyncio
async def test_double_revert_prevented(
    integration_session: AsyncSession,
) -> None:
    """Test that calling revert twice doesn't drive reserved_balance negative.

    This simulates the double-revert scenario where both upstream/base.py
    and proxy.py attempt to revert the same reservation.
    """
    from routstr.auth import revert_pay_for_request

    unique_key = f"test_double_revert_{uuid.uuid4().hex[:8]}"
    test_key = ApiKey(
        hashed_key=unique_key,
        balance=10000,
        reserved_balance=500,
        total_requests=5,
    )
    integration_session.add(test_key)
    await integration_session.commit()

    # First revert — should succeed
    result1 = await revert_pay_for_request(test_key, integration_session, 500)
    await integration_session.refresh(test_key)

    assert result1 is True
    assert test_key.reserved_balance == 0
    assert test_key.total_requests == 4

    # Second revert of the same amount — should be no-op
    result2 = await revert_pay_for_request(test_key, integration_session, 500)
    await integration_session.refresh(test_key)

    assert result2 is False, "Second revert should be a no-op"
    assert test_key.reserved_balance == 0, (
        f"Reserved balance should stay 0, got: {test_key.reserved_balance}"
    )
    assert test_key.total_requests == 4, (
        f"Total requests should stay 4, got: {test_key.total_requests}"
    )


@pytest.mark.asyncio
async def test_sequential_reverts_never_go_negative(
    integration_session: AsyncSession,
) -> None:
    """Test that multiple reverts don't cause negative reserved_balance.

    Simulates the double-revert scenario where multiple code paths
    attempt to revert the same reservation.
    """
    from routstr.auth import revert_pay_for_request

    unique_key = f"test_multi_revert_{uuid.uuid4().hex[:8]}"
    test_key = ApiKey(
        hashed_key=unique_key,
        balance=10000,
        reserved_balance=500,
        total_requests=5,
    )
    integration_session.add(test_key)
    await integration_session.commit()

    # Run 5 sequential reverts for the same 500 reservation
    results = []
    for _ in range(5):
        r = await revert_pay_for_request(test_key, integration_session, 500)
        results.append(r)

    await integration_session.refresh(test_key)

    # Exactly one should succeed, rest should be no-ops
    success_count = sum(1 for r in results if r is True)
    assert success_count == 1, (
        f"Exactly one revert should succeed, got {success_count} successes"
    )
    assert test_key.reserved_balance == 0, (
        f"Reserved balance should be 0, got: {test_key.reserved_balance}"
    )
    assert test_key.reserved_balance >= 0, (
        f"Reserved balance went negative: {test_key.reserved_balance}"
    )


@pytest.mark.asyncio
async def test_child_key_revert_floor_guard(
    integration_session: AsyncSession,
) -> None:
    """Test that child key reserved_balance also has floor guard on revert."""
    from routstr.auth import revert_pay_for_request

    parent_key_hash = f"test_parent_{uuid.uuid4().hex[:8]}"
    child_key_hash = f"test_child_{uuid.uuid4().hex[:8]}"

    parent_key = ApiKey(
        hashed_key=parent_key_hash,
        balance=10000,
        reserved_balance=500,
        total_requests=3,
    )
    child_key = ApiKey(
        hashed_key=child_key_hash,
        balance=0,
        reserved_balance=500,
        total_requests=3,
        parent_key_hash=parent_key_hash,
    )
    integration_session.add(parent_key)
    integration_session.add(child_key)
    await integration_session.commit()

    # First revert succeeds
    result1 = await revert_pay_for_request(child_key, integration_session, 500)
    await integration_session.refresh(parent_key)
    await integration_session.refresh(child_key)

    assert result1 is True
    assert parent_key.reserved_balance == 0
    assert child_key.reserved_balance == 0

    # Second revert is a no-op for both parent and child
    result2 = await revert_pay_for_request(child_key, integration_session, 500)
    await integration_session.refresh(parent_key)
    await integration_session.refresh(child_key)

    assert result2 is False
    assert parent_key.reserved_balance == 0, (
        f"Parent reserved_balance should stay 0, got: {parent_key.reserved_balance}"
    )
    assert child_key.reserved_balance == 0, (
        f"Child reserved_balance should stay 0, got: {child_key.reserved_balance}"
    )
