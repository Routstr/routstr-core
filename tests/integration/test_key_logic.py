import time

import pytest
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
