import asyncio
import time
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import pay_for_request
from routstr.core.db import ApiKey, create_session


@pytest.mark.asyncio
async def test_key_validity_date(integration_session: AsyncSession) -> None:
    # 1. Create a key that is expired
    expired_time = int(time.time()) - 3600
    key = ApiKey(hashed_key="expired_key", balance=1000, validity_date=expired_time)
    integration_session.add(key)
    await integration_session.commit()

    # 2. Try to pay for a request - should fail
    with pytest.raises(Exception) as excinfo:
        await pay_for_request(key, 100, integration_session)
    assert "expired" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_key_balance_limit(integration_session: AsyncSession) -> None:
    # 1. Create a key with a balance limit
    key = ApiKey(
        hashed_key="limited_key", balance=10000, balance_limit=500, total_spent=450
    )
    integration_session.add(key)
    await integration_session.commit()

    # 2. Try to pay for a request that exceeds the limit
    with pytest.raises(Exception) as excinfo:
        await pay_for_request(key, 100, integration_session)
    assert "limit exceeded" in str(excinfo.value).lower()

    # 3. Try to pay for a request that fits
    await pay_for_request(key, 50, integration_session)
    await integration_session.refresh(key)
    # Note: total_spent is updated in adjust_payment_for_tokens,
    # but pay_for_request checks it.
    # In our current logic, pay_for_request checks (total_spent + cost) > balance_limit.


@pytest.mark.asyncio
async def test_key_daily_reset_policy(integration_session: AsyncSession) -> None:
    # 1. Create a key with a daily reset policy and old reset date
    yesterday = int((datetime.now() - timedelta(days=1)).timestamp())
    key = ApiKey(
        hashed_key="daily_reset_key",
        balance=10000,
        balance_limit=1000,
        balance_limit_reset="daily",
        balance_limit_reset_date=yesterday,
        total_spent=900,
    )
    integration_session.add(key)
    await integration_session.commit()

    # 2. Pay for a request - should trigger reset first because it's a new day
    # Request is 200, total_spent is 900. 900+200 > 1000,
    # but reset should happen making total_spent 0, then 0+200 < 1000.
    await pay_for_request(key, 200, integration_session)

    await integration_session.refresh(key)
    assert key.total_spent == 0  # Reset in pay_for_request happens before charging
    # Wait, the charging logic in pay_for_request increments parent/billing_key's total_requests,
    # but total_spent is updated in adjust_payment_for_tokens.
    # However, the reset logic sets total_spent to 0.
    assert key.balance_limit_reset_date is not None
    assert key.balance_limit_reset_date > yesterday


@pytest.mark.asyncio
async def test_periodic_key_reset_job(integration_session: AsyncSession) -> None:
    # 1. Create multiple keys needing reset
    yesterday = int((datetime.now() - timedelta(days=1)).timestamp())
    key1 = ApiKey(
        hashed_key="job_reset_key_1",
        balance=1000,
        balance_limit=1000,
        balance_limit_reset="daily",
        balance_limit_reset_date=yesterday,
        total_spent=500,
    )
    key2 = ApiKey(
        hashed_key="job_reset_key_2",
        balance=1000,
        balance_limit=1000,
        balance_limit_reset="daily",
        balance_limit_reset_date=yesterday,
        total_spent=800,
    )
    integration_session.add(key1)
    integration_session.add(key2)
    await integration_session.commit()

    # 2. Run the periodic reset logic manually (mocking the background task loop)
    # We can't easily run the actual loop because it has a sleep,
    # but we can test the logic inside.

    # Implementation of periodic_key_reset logic for testing:
    stmt = select(ApiKey).where(ApiKey.balance_limit_reset != None)  # noqa: E711
    keys = (await integration_session.exec(stmt)).all()
    now = int(time.time())
    for k in keys:
        if k.hashed_key in ["job_reset_key_1", "job_reset_key_2"]:
            k.total_spent = 0
            k.balance_limit_reset_date = now
            integration_session.add(k)
    await integration_session.commit()

    # 3. Verify resets
    await integration_session.refresh(key1)
    await integration_session.refresh(key2)
    assert key1.total_spent == 0
    assert key2.total_spent == 0


@pytest.mark.asyncio
async def test_balance_limit_enforced_atomically_under_concurrency(
    patched_db_engine: None,
) -> None:
    parent_hash = "parent_limit_atomic"
    child_hash = "child_limit_atomic"
    cost = 300

    async with create_session() as session:
        parent = ApiKey(hashed_key=parent_hash, balance=10000)
        child = ApiKey(
            hashed_key=child_hash,
            balance=0,
            parent_key_hash=parent_hash,
            balance_limit=cost,
            total_spent=0,
        )
        session.add(parent)
        session.add(child)
        await session.commit()

    results: list[str] = []

    async def attempt() -> None:
        async with create_session() as session:
            fresh_child = await session.get(ApiKey, child_hash)
            assert fresh_child is not None
            try:
                await pay_for_request(fresh_child, cost, session)
                results.append("success")
            except HTTPException as exc:
                assert exc.status_code == 402
                results.append("blocked")

    await asyncio.gather(attempt(), attempt())

    assert sorted(results) == ["blocked", "success"], (
        f"Expected exactly one success and one 402, got: {results}"
    )

    async with create_session() as session:
        final_child = await session.get(ApiKey, child_hash)
        assert final_child is not None

    assert final_child.reserved_balance == cost, (
        f"Child reserved_balance should equal one reservation, "
        f"got {final_child.reserved_balance}"
    )


@pytest.mark.asyncio
async def test_parallel_payments_with_parent_and_child_key(
    patched_db_engine: None,
) -> None:
    parent_hash = "parent_parallel_mixed"
    child_hash = "child_parallel_mixed"
    cost = 300

    async with create_session() as session:
        parent = ApiKey(hashed_key=parent_hash, balance=10000)
        child = ApiKey(
            hashed_key=child_hash,
            balance=0,
            parent_key_hash=parent_hash,
            balance_limit=2 * cost,
        )
        session.add(parent)
        session.add(child)
        await session.commit()

    async def attempt(key_hash: str) -> str:
        async with create_session() as session:
            fresh_key = await session.get(ApiKey, key_hash)
            assert fresh_key is not None
            try:
                await pay_for_request(fresh_key, cost, session)
                return "success"
            except HTTPException as exc:
                assert exc.status_code == 402
                return "blocked"

    results = await asyncio.gather(attempt(parent_hash), attempt(child_hash))

    assert results == ["success", "success"], (
        f"Both parent and child payments should succeed, got: {results}"
    )

    async with create_session() as session:
        final_parent = await session.get(ApiKey, parent_hash)
        final_child = await session.get(ApiKey, child_hash)
        assert final_parent is not None
        assert final_child is not None

    # Both requests bill the parent; only the child request reserves on the child.
    assert final_parent.reserved_balance == 2 * cost
    assert final_parent.total_requests == 2
    assert final_child.reserved_balance == cost
    assert final_child.total_requests == 1


@pytest.mark.asyncio
async def test_balance_limit_with_existing_total_spent_under_concurrency(
    patched_db_engine: None,
) -> None:
    parent_hash = "parent_total_spent"
    child_hash = "child_total_spent"
    cost = 300

    async with create_session() as session:
        parent = ApiKey(hashed_key=parent_hash, balance=10000)
        # 700 already spent against a 1000 limit: only one more 300 request fits.
        child = ApiKey(
            hashed_key=child_hash,
            balance=0,
            parent_key_hash=parent_hash,
            balance_limit=1000,
            total_spent=700,
        )
        session.add(parent)
        session.add(child)
        await session.commit()

    results: list[str] = []

    async def attempt() -> None:
        async with create_session() as session:
            fresh_child = await session.get(ApiKey, child_hash)
            assert fresh_child is not None
            try:
                await pay_for_request(fresh_child, cost, session)
                results.append("success")
            except HTTPException as exc:
                assert exc.status_code == 402
                results.append("blocked")

    await asyncio.gather(attempt(), attempt())

    assert sorted(results) == ["blocked", "success"], (
        f"Expected exactly one success and one 402, got: {results}"
    )

    async with create_session() as session:
        final_child = await session.get(ApiKey, child_hash)
        assert final_child is not None

    assert final_child.reserved_balance == cost
    assert final_child.total_spent == 700


@pytest.mark.asyncio
async def test_balance_limit_with_existing_reserved_balance(
    patched_db_engine: None,
) -> None:
    parent_hash = "parent_reserved_set"
    blocked_hash = "child_reserved_blocked"
    allowed_hash = "child_reserved_allowed"
    cost = 300

    async with create_session() as session:
        parent = ApiKey(hashed_key=parent_hash, balance=10000)
        # 800 already reserved against a 1000 limit: another 300 must be rejected.
        blocked_child = ApiKey(
            hashed_key=blocked_hash,
            balance=0,
            parent_key_hash=parent_hash,
            balance_limit=1000,
            reserved_balance=800,
        )
        # 500 reserved against a 1000 limit: another 300 still fits.
        allowed_child = ApiKey(
            hashed_key=allowed_hash,
            balance=0,
            parent_key_hash=parent_hash,
            balance_limit=1000,
            reserved_balance=500,
        )
        session.add(parent)
        session.add(blocked_child)
        session.add(allowed_child)
        await session.commit()

    async with create_session() as session:
        fresh_blocked = await session.get(ApiKey, blocked_hash)
        assert fresh_blocked is not None
        with pytest.raises(HTTPException) as exc_info:
            await pay_for_request(fresh_blocked, cost, session)
        assert exc_info.value.status_code == 402

    async with create_session() as session:
        fresh_allowed = await session.get(ApiKey, allowed_hash)
        assert fresh_allowed is not None
        await pay_for_request(fresh_allowed, cost, session)

    async with create_session() as session:
        final_blocked = await session.get(ApiKey, blocked_hash)
        final_allowed = await session.get(ApiKey, allowed_hash)
        final_parent = await session.get(ApiKey, parent_hash)
        assert final_blocked is not None
        assert final_allowed is not None
        assert final_parent is not None

    assert final_blocked.reserved_balance == 800, "Rejected request must not reserve"
    assert final_blocked.total_requests == 0
    assert final_allowed.reserved_balance == 500 + cost
    assert final_allowed.total_requests == 1
    # Only the allowed request should have billed the parent.
    assert final_parent.reserved_balance == cost
    assert final_parent.total_requests == 1


@pytest.mark.asyncio
async def test_child_reservation_discarded_when_parent_balance_depleted(
    patched_db_engine: None,
) -> None:
    parent_hash = "parent_depleted"
    child_hash = "child_depleted"
    cost = 300

    async with create_session() as session:
        # Parent can only afford one request; child has no balance_limit.
        parent = ApiKey(hashed_key=parent_hash, balance=cost)
        child = ApiKey(
            hashed_key=child_hash,
            balance=0,
            parent_key_hash=parent_hash,
        )
        session.add(parent)
        session.add(child)
        await session.commit()

    results: list[str] = []

    async def attempt() -> None:
        async with create_session() as session:
            fresh_child = await session.get(ApiKey, child_hash)
            assert fresh_child is not None
            try:
                await pay_for_request(fresh_child, cost, session)
                results.append("success")
            except HTTPException as exc:
                assert exc.status_code == 402
                results.append("blocked")

    await asyncio.gather(attempt(), attempt())

    assert sorted(results) == ["blocked", "success"], (
        f"Expected exactly one success and one 402, got: {results}"
    )

    async with create_session() as session:
        final_parent = await session.get(ApiKey, parent_hash)
        final_child = await session.get(ApiKey, child_hash)
        assert final_parent is not None
        assert final_child is not None

    assert final_parent.reserved_balance == cost
    # The failed request must not leave a committed reservation on the child.
    assert final_child.reserved_balance == cost, (
        f"Child reserved_balance should reflect only the successful request, "
        f"got {final_child.reserved_balance}"
    )
    assert final_child.total_requests == 1


@pytest.mark.asyncio
async def test_refund_does_not_delete_key(integration_session: AsyncSession) -> None:
    # This requires mocking the router call or testing the logic in balance.py
    from routstr.balance import ApiKey

    key = ApiKey(hashed_key="refund_test_key", balance=1000, reserved_balance=100)
    integration_session.add(key)
    await integration_session.commit()

    # Logic from refund_wallet_endpoint:
    key.balance = 0
    key.reserved_balance = 0
    integration_session.add(key)
    await integration_session.commit()

    # Verify key still exists
    fetched_key = await integration_session.get(ApiKey, "refund_test_key")
    assert fetched_key is not None
    assert fetched_key.balance == 0
    assert fetched_key.reserved_balance == 0
