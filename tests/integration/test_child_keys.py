import pytest
import secrets
from fastapi import HTTPException
from routstr.core.db import ApiKey
from routstr.balance import create_child_key
from routstr.auth import pay_for_request, adjust_payment_for_tokens
from routstr.core.settings import settings


@pytest.mark.asyncio
async def test_child_key_flow(integration_session):
    # 1. Create a parent key with balance
    parent_raw = "parent_test_key_" + secrets.token_hex(4)
    parent_key = ApiKey(
        hashed_key=parent_raw,
        balance=10000,  # 10 sats
    )
    integration_session.add(parent_key)
    await integration_session.commit()
    await integration_session.refresh(parent_key)

    # Mock settings
    settings.child_key_cost = 1000  # 1 sat

    # 2. Call create_child_key
    result = await create_child_key(parent_key, integration_session)

    assert "api_key" in result
    assert result["cost_msats"] == 1000
    assert result["parent_balance"] == 9000

    child_key_raw = result["api_key"][3:]  # remove sk-

    # 3. Verify child key exists in DB
    child_key_db = await integration_session.get(ApiKey, child_key_raw)
    assert child_key_db is not None
    assert child_key_db.parent_key_hash == parent_key.hashed_key
    assert child_key_db.balance == 0

    # 4. Test payment with child key
    cost = 500
    await pay_for_request(child_key_db, cost, integration_session)

    # Refresh keys
    await integration_session.refresh(parent_key)
    await integration_session.refresh(child_key_db)

    # Parent should be charged
    assert parent_key.reserved_balance == 500
    assert parent_key.total_requests == 1

    # Child should have total_requests incremented
    assert child_key_db.total_requests == 1

    # 5. Test adjustment
    response_data = {"model": "test-model", "usage": {"total_tokens": 10}}

    # Mock calculate_cost
    import routstr.auth
    from routstr.payment.cost_calculation import CostData

    async def mock_calculate_cost(*args, **kwargs):
        return CostData(
            base_msats=0, input_msats=200, output_msats=200, total_msats=400
        )

    # Patch calculate_cost
    original_calculate_cost = routstr.auth.calculate_cost
    routstr.auth.calculate_cost = mock_calculate_cost

    try:
        adjustment = await adjust_payment_for_tokens(
            child_key_db, response_data, integration_session, 500
        )
        assert adjustment["total_msats"] == 400

        # Refresh keys
        await integration_session.refresh(parent_key)
        await integration_session.refresh(child_key_db)

        # Parent should have updated balance and total_spent
        assert parent_key.reserved_balance == 0
        assert parent_key.balance == 9000 - 400
        assert (
            parent_key.total_spent == 1400
        )  # 1000 for child key creation + 400 for request

        # Child should also have total_spent updated
        assert child_key_db.total_spent == 400

    finally:
        routstr.auth.calculate_cost = original_calculate_cost


@pytest.mark.asyncio
async def test_child_key_insufficient_balance(integration_session):
    parent_key = ApiKey(
        hashed_key="poor_parent_" + secrets.token_hex(4),
        balance=500,
    )
    integration_session.add(parent_key)
    await integration_session.commit()
    await integration_session.refresh(parent_key)

    settings.child_key_cost = 1000

    with pytest.raises(HTTPException) as exc:
        await create_child_key(parent_key, integration_session)
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_child_key_cannot_create_child(integration_session):
    parent_key = ApiKey(
        hashed_key="parent_" + secrets.token_hex(4),
        balance=10000,
    )
    child_key = ApiKey(
        hashed_key="child_" + secrets.token_hex(4),
        balance=0,
        parent_key_hash=parent_key.hashed_key,
    )
    integration_session.add(parent_key)
    integration_session.add(child_key)
    await integration_session.commit()
    await integration_session.refresh(child_key)

    with pytest.raises(HTTPException) as exc:
        await create_child_key(child_key, integration_session)
    assert exc.value.status_code == 400
    assert "Cannot create a child key for another child key" in str(exc.value.detail)
