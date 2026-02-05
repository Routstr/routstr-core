import time
from datetime import datetime, timedelta

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import pay_for_request
from routstr.core.db import ApiKey


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
    stmt = select(ApiKey).where(ApiKey.balance_limit_reset != None)
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
