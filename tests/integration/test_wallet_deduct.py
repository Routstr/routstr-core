"""
Integration tests for wallet balance deduction functionality.
Tests POST /v1/balance/deduct (and deprecated /v1/wallet/deduct) with various scenarios.
"""

from __future__ import annotations

import math
from typing import Any, List

import pytest
from httpx import AsyncClient
from sqlmodel import select, update

from routstr.core.db import ApiKey

from .utils import ConcurrencyTester, ResponseValidator


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deduct_success_reduces_balance_and_increments_total_spent(
    authenticated_client: AsyncClient, integration_session: Any
) -> None:
    """Deduct with sufficient balance should succeed and return new balance.
    Verifies DB reflects balance decrease and total_spent increase.
    """
    # Get initial wallet info
    info_resp = await authenticated_client.get("/v1/wallet/")
    assert info_resp.status_code == 200
    info = info_resp.json()
    api_key = info["api_key"]
    initial_balance = int(info["balance"])  # msats

    # Deduct a specific amount
    amount = 123_456
    resp = await authenticated_client.post("/v1/balance/deduct", json={"msats": amount})
    assert resp.status_code == 200
    data = resp.json()
    assert "balance" in data
    assert isinstance(data["balance"], int)

    # Validate new balance
    expected_balance = initial_balance - amount
    assert data["balance"] == expected_balance

    # Verify DB state
    hashed_key = api_key[3:] if api_key.startswith("sk-") else api_key
    result = await integration_session.execute(
        select(ApiKey).where(ApiKey.hashed_key == hashed_key)  # type: ignore[arg-type]
    )
    key_obj = result.scalar_one()
    assert key_obj.balance == expected_balance
    assert key_obj.total_spent >= amount  # in case other charges applied elsewhere


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deduct_with_insufficient_balance_returns_402(
    authenticated_client: AsyncClient,
) -> None:
    """Deducting more than available (unreserved) balance should return 402 with error payload."""
    # Fresh client has 10k sats => 10,000,000 msats
    info_resp = await authenticated_client.get("/v1/wallet/")
    assert info_resp.status_code == 200
    initial_balance = int(info_resp.json()["balance"])  # msats

    # Attempt to deduct more than available
    amount = initial_balance + 1_000  # ensure strictly greater than available
    resp = await authenticated_client.post("/v1/balance/deduct", json={"msats": amount})

    assert resp.status_code == 402
    body = resp.json()
    assert "detail" in body
    assert "error" in body["detail"]
    err = body["detail"]["error"]
    assert err.get("type") == "insufficient_quota"
    assert err.get("code") == "insufficient_balance"
    assert "Insufficient balance" in err.get("message", "")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_amount", [0, -1, -1000])
async def test_deduct_with_non_positive_amount_returns_400(
    authenticated_client: AsyncClient, bad_amount: int
) -> None:
    """msats must be a positive integer."""
    resp = await authenticated_client.post("/v1/balance/deduct", json={"msats": bad_amount})
    assert resp.status_code == 400
    assert resp.json().get("detail") == "msats must be a positive integer"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deduct_respects_reserved_balance(
    authenticated_client: AsyncClient, integration_session: Any
) -> None:
    """Deduction should consider reserved_balance and fail if amount > available (balance - reserved)."""
    # Get API key and initial state
    info_resp = await authenticated_client.get("/v1/wallet/")
    assert info_resp.status_code == 200
    info = info_resp.json()
    api_key = info["api_key"]
    initial_balance = int(info["balance"])  # msats

    # Artificially set reserved_balance to reduce availability
    reserved = 2_000_000  # 2M msats reserved
    hashed_key = api_key[3:] if api_key.startswith("sk-") else api_key
    await integration_session.execute(
        update(ApiKey)
        .where(ApiKey.hashed_key == hashed_key)  # type: ignore[arg-type]
        .values(reserved_balance=reserved)
    )
    await integration_session.commit()

    # Deduct more than available (should fail)
    amount_too_high = reserved + (initial_balance - reserved) + 1  # > available
    resp_fail = await authenticated_client.post(
        "/v1/balance/deduct", json={"msats": amount_too_high}
    )
    assert resp_fail.status_code == 402

    # Deduct within available (should succeed)
    amount_ok = (initial_balance - reserved) - 123_000  # leave some remainder
    resp_ok = await authenticated_client.post(
        "/v1/balance/deduct", json={"msats": amount_ok}
    )
    assert resp_ok.status_code == 200
    new_balance = resp_ok.json()["balance"]
    assert new_balance == initial_balance - amount_ok

    # Verify reserved unchanged and total_spent increments
    result = await integration_session.execute(
        select(ApiKey).where(ApiKey.hashed_key == hashed_key)  # type: ignore[arg-type]
    )
    key_obj = result.scalar_one()
    assert key_obj.reserved_balance == reserved
    assert key_obj.balance == new_balance
    assert key_obj.total_spent >= amount_ok


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deduct_is_atomic_with_concurrent_requests(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Concurrent deductions should be atomic and not overspend.
    We expect floor(total_balance / per_request) successes and the rest 402.
    """
    # Read fresh balance
    info_resp = await authenticated_client.get("/v1/wallet/")
    assert info_resp.status_code == 200
    total_balance = int(info_resp.json()["balance"])  # msats

    per_request = 600_000  # 0.6M msats per request
    expected_successes = total_balance // per_request

    # Build concurrent requests
    reqs: List[dict[str, Any]] = []
    for _ in range(20):
        reqs.append(
            {
                "method": "POST",
                "url": "/v1/balance/deduct",
                "json": {"msats": per_request},
                "headers": {
                    "Authorization": authenticated_client.headers["Authorization"]
                },
            }
        )

    tester = ConcurrencyTester()
    responses = await tester.run_concurrent_requests(integration_client, reqs, max_concurrent=10)

    ok = [r for r in responses if r.status_code == 200]
    errs = [r for r in responses if r.status_code == 402]
    others = [r for r in responses if r.status_code not in (200, 402)]

    # We should have at most expected_successes successes
    assert len(ok) <= expected_successes
    # And at least one error if we attempted to spend more than available
    assert len(errs) >= max(0, len(responses) - expected_successes)
    # No other unexpected statuses
    assert not others


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deduct_requires_valid_auth(integration_client: AsyncClient) -> None:
    """Missing or invalid Authorization should fail."""
    # Missing header -> FastAPI validation error (422)
    resp_missing = await integration_client.post("/v1/balance/deduct", json={"msats": 1000})
    assert resp_missing.status_code in (401, 422)

    # Invalid bearer format/key -> 401 from validate_bearer_key
    resp_invalid = await integration_client.post(
        "/v1/balance/deduct",
        json={"msats": 1000},
        headers={"Authorization": "Bearer invalid-key"},
    )
    # Validate error response
    validator = ResponseValidator()
    result = validator.validate_error_response(resp_invalid, expected_status=401, expected_error_key="detail")
    assert result["valid"], f"Unexpected error response: {resp_invalid.text}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deduct_deprecated_wallet_route(authenticated_client: AsyncClient) -> None:
    """Deprecated route /v1/wallet/deduct should behave identically to /v1/balance/deduct."""
    # Get initial state
    info_resp = await authenticated_client.get("/v1/wallet/")
    assert info_resp.status_code == 200
    initial_balance = int(info_resp.json()["balance"])  # msats

    amount = 111_000
    resp = await authenticated_client.post("/v1/wallet/deduct", json={"msats": amount})
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance"] == initial_balance - amount
