"""
Tests showing how a user hits "Insufficient balance: X mSats required for this model"
when their balance is too low for the model's cost.

The log line that triggered this:
  WARNING  Insufficient billing balance during validation
  ERROR    Bearer token validation failed: HTTPException: 402:
           {'error': {'message': 'Insufficient balance: 622888 mSats required
           for this model. 20320 available.', ...}}

This happens in validate_bearer_key (auth.py) when:
  billing_key.total_balance < min_cost   (model's max cost)

and also in pay_for_request when the atomic UPDATE finds no available balance.
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey


def _key(balance: int, reserved: int = 0) -> ApiKey:
    return ApiKey(
        hashed_key=f"test_{uuid.uuid4().hex}",
        balance=balance,
        reserved_balance=reserved,
        total_spent=0,
    )


# ---------------------------------------------------------------------------
# Test 1 — simplest case: balance < model cost → pay_for_request raises 402
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pay_for_request_raises_402_when_balance_too_low(
    integration_session: AsyncSession,
) -> None:
    """
    User has 20_000 msats. Model costs 622_888 msats.
    pay_for_request must raise HTTP 402 with a clear message.
    """
    from routstr.auth import pay_for_request

    model_cost = 622_888
    user_balance = 20_000

    key = _key(balance=user_balance)
    integration_session.add(key)
    await integration_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await pay_for_request(key, model_cost, integration_session)

    assert exc_info.value.status_code == 402
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    error = detail["error"]
    assert error["code"] == "insufficient_balance"
    assert str(model_cost) in error["message"]
    assert str(user_balance) in error["message"]

    # Balance must be untouched
    await integration_session.refresh(key)
    assert key.balance == user_balance
    assert key.reserved_balance == 0


# ---------------------------------------------------------------------------
# Test 2 — balance is zero
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pay_for_request_raises_402_on_zero_balance(
    integration_session: AsyncSession,
) -> None:
    """User with zero balance cannot make any request."""
    from routstr.auth import pay_for_request

    key = _key(balance=0)
    integration_session.add(key)
    await integration_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await pay_for_request(key, 1_000, integration_session)

    assert exc_info.value.status_code == 402
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["code"] == "insufficient_balance"


# ---------------------------------------------------------------------------
# Test 3 — all balance is reserved (total_balance = balance - reserved = 0)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pay_for_request_raises_402_when_all_balance_reserved(
    integration_session: AsyncSession,
) -> None:
    """
    User has 50_000 msats balance but 50_000 is already reserved for in-flight
    requests. Free balance (total_balance) = 0. Should get 402.
    """
    from routstr.auth import pay_for_request

    key = _key(balance=50_000, reserved=50_000)
    integration_session.add(key)
    await integration_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await pay_for_request(key, 1_000, integration_session)

    assert exc_info.value.status_code == 402
    # Balance and reserved must be untouched
    await integration_session.refresh(key)
    assert key.balance == 50_000
    assert key.reserved_balance == 50_000


# ---------------------------------------------------------------------------
# Test 4 — balance just one msat below model cost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pay_for_request_raises_402_one_msat_short(
    integration_session: AsyncSession,
) -> None:
    """Off-by-one: balance is exactly model_cost - 1."""
    from routstr.auth import pay_for_request

    model_cost = 10_000
    key = _key(balance=model_cost - 1)
    integration_session.add(key)
    await integration_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await pay_for_request(key, model_cost, integration_session)

    assert exc_info.value.status_code == 402
    await integration_session.refresh(key)
    assert key.balance == model_cost - 1   # untouched
    assert key.reserved_balance == 0


# ---------------------------------------------------------------------------
# Test 5 — balance exactly equal to model cost → succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pay_for_request_succeeds_when_balance_equals_cost(
    integration_session: AsyncSession,
) -> None:
    """Balance == model cost: the request should be reserved successfully."""
    from routstr.auth import pay_for_request

    model_cost = 10_000
    key = _key(balance=model_cost)
    integration_session.add(key)
    await integration_session.commit()

    # Should not raise
    await pay_for_request(key, model_cost, integration_session)

    await integration_session.refresh(key)
    assert key.reserved_balance == model_cost
    assert key.balance == model_cost   # balance unchanged, only reserved goes up


# ---------------------------------------------------------------------------
# Test 6 — HTTP layer returns 402 JSON with the right shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_402_response_shape_on_insufficient_balance(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """
    End-to-end: POST /v1/chat/completions with a key whose balance is far below
    the mocked model cost returns HTTP 402 with the expected JSON error body.

    Matches exactly the log snippet in the bug report:
      'Insufficient balance: X mSats required for this model. Y available.'
    """
    from unittest.mock import AsyncMock, MagicMock

    model_cost = 622_888
    user_balance = 20_320

    key = _key(balance=user_balance)
    integration_session.add(key)
    await integration_session.commit()

    # Minimal model stub so proxy routing doesn't 400 before reaching balance check
    mock_model = MagicMock()
    mock_model.sats_pricing = None

    # Upstream stub — never reached because balance check fires first
    mock_upstream = MagicMock()
    mock_upstream.prepare_headers = MagicMock(return_value={})

    with (
        patch("routstr.proxy.get_model_instance", return_value=mock_model),
        patch("routstr.proxy.get_provider_for_model", return_value=[mock_upstream]),
        # Patch where it is used (proxy imports it at module level)
        patch(
            "routstr.proxy.get_max_cost_for_model",
            new=AsyncMock(return_value=model_cost),
        ),
    ):
        response = await integration_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer sk-{key.hashed_key}"},
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 402
    body = response.json()
    # FastAPI wraps HTTPException detail under "detail"
    error = body["detail"]["error"]
    assert error["code"] == "insufficient_balance"
    assert error["type"] == "insufficient_quota"
    assert str(model_cost) in error["message"]
    assert str(user_balance) in error["message"]

    # Balance must be completely untouched
    await integration_session.refresh(key)
    assert key.balance == user_balance
    assert key.reserved_balance == 0
    assert key.total_spent == 0
