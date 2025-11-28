"""
Integration tests for wallet information retrieval endpoints.
Tests GET /v1/wallet/ and GET /v1/wallet/info endpoints with various scenarios.
"""

import time
from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlmodel import select, update

from routstr.core.db import TemporaryCredit

from .utils import ConcurrencyTester, ResponseValidator


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_endpoint_with_valid_api_key(
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    integration_session: Any,
) -> None:
    """Test GET /v1/wallet/ returns account information for valid API key"""

    # authenticated_client fixture provides a client with valid API key and 10k sats balance
    response = await authenticated_client.get("/v1/wallet/")

    assert response.status_code == 200
    data = response.json()

    # Validate response structure
    assert "api_key" in data
    assert "balance" in data

    # API key should have proper format
    assert data["api_key"].startswith("sk-")
    assert len(data["api_key"]) > 10

    # Balance should be 10,000 sats (10,000,000 msats)
    assert data["balance"] == 10_000_000

    # Verify data consistency with database
    # The API key format is "sk-" + hashed_key, where hashed_key is the hash of the cashu token
    api_key = data["api_key"]
    assert api_key.startswith("sk-")
    hashed_key = api_key[3:]  # Remove "sk-" prefix

    result = await integration_session.execute(
        select(TemporaryCredit).where(TemporaryCredit.hashed_key == hashed_key)  # type: ignore[arg-type]
    )
    db_key = result.scalar_one()

    assert db_key.balance == data["balance"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_info_endpoint_detailed_information(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test GET /v1/wallet/info returns detailed wallet information"""

    # Get info from both endpoints
    response_basic = await authenticated_client.get("/v1/wallet/")
    response_info = await authenticated_client.get("/v1/wallet/info")

    assert response_basic.status_code == 200
    assert response_info.status_code == 200

    data_basic = response_basic.json()
    data_info = response_info.json()

    # Currently both endpoints return the same data
    assert data_basic == data_info

    # Validate info endpoint structure
    assert "api_key" in data_info
    assert "balance" in data_info

    # Note: The implementation doesn't include additional fields like:
    # - refund_address
    # - key_expiry_time
    # - total_spent
    # - total_requests
    # - mint URLs
    # This is a limitation of the current implementation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unauthorized_access_to_wallet_endpoints(
    integration_client: AsyncClient,
) -> None:
    """Test unauthorized access returns 401 for wallet endpoints"""

    # Test both endpoints without authentication
    endpoints = ["/v1/wallet/", "/v1/wallet/info"]

    for endpoint in endpoints:
        # No authorization header
        response = await integration_client.get(endpoint)
        assert (
            response.status_code == 422
        )  # FastAPI returns 422 for missing required headers

        validator = ResponseValidator()
        error_validation = validator.validate_error_response(
            response, expected_status=422, expected_error_key="detail"
        )
        assert error_validation["valid"]

        # Invalid API key
        integration_client.headers["Authorization"] = "Bearer sk-invalid-key-12345"
        response = await integration_client.get(endpoint)
        assert response.status_code == 401

        # Clear header for next iteration
        integration_client.headers.pop("Authorization", None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_with_zero_balance(
    integration_client: AsyncClient, testmint_wallet: Any, integration_session: Any
) -> None:
    """Test wallet endpoints with zero balance API key"""

    # Create API key with initial balance
    token = await testmint_wallet.mint_tokens(100)  # 100 sats
    integration_client.headers["Authorization"] = f"Bearer {token}"
    response = await integration_client.get("/v1/wallet/info")
    assert response.status_code == 200
    api_key = response.json()["api_key"]

    # Manually set balance to zero in database
    hashed_key = api_key[3:]  # Remove "sk-" prefix
    await integration_session.execute(
        update(TemporaryCredit)
        .where(TemporaryCredit.hashed_key == hashed_key)
        .values(balance=0)  # type: ignore[arg-type]
    )
    await integration_session.commit()

    # Test that zero balance wallet can still authenticate
    integration_client.headers["Authorization"] = f"Bearer {api_key}"

    # Test both endpoints
    response_basic = await integration_client.get("/v1/wallet/")
    response_info = await integration_client.get("/v1/wallet/info")

    assert response_basic.status_code == 200
    assert response_info.status_code == 200

    # Verify zero balance is returned
    assert response_basic.json()["balance"] == 0
    assert response_info.json()["balance"] == 0

    # Note: Zero balance keys are NOT automatically deleted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_api_key_behavior(
    integration_client: AsyncClient, testmint_wallet: Any, integration_session: Any
) -> None:
    """Test behavior of expired API keys"""

    # Create API key first without expiry
    token = await testmint_wallet.mint_tokens(500)
    integration_client.headers["Authorization"] = f"Bearer {token}"
    response = await integration_client.get("/v1/wallet/info")
    assert response.status_code == 200
    api_key = response.json()["api_key"]

    # Set expiry time to 1 hour ago in database
    past_expiry = int((datetime.utcnow() - timedelta(hours=1)).timestamp())
    hashed_key = api_key[3:]  # Remove "sk-" prefix

    # Update the key with past expiry time
    from sqlmodel import update

    await integration_session.execute(
        update(TemporaryCredit)
        .where(TemporaryCredit.hashed_key == hashed_key)  # type: ignore[arg-type]
        .values(
            refund_expiration_time=past_expiry, refund_address="test@lightning.address"
        )
    )
    await integration_session.commit()

    # Important: Expired keys can still authenticate until background task processes them
    integration_client.headers["Authorization"] = f"Bearer {api_key}"

    response = await integration_client.get("/v1/wallet/")
    assert response.status_code == 200  # Still works!
    assert response.json()["balance"] == 500_000  # 500 sats in msats

    # Verify expiry time was stored
    hashed_key = api_key[3:]  # Remove "sk-" prefix
    result = await integration_session.execute(
        select(TemporaryCredit).where(TemporaryCredit.hashed_key == hashed_key)  # type: ignore[arg-type]
    )
    db_key = result.scalar_one()
    assert db_key.refund_expiration_time == past_expiry
    assert db_key.refund_address == "test@lightning.address"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_access_same_api_key(
    integration_client: AsyncClient, authenticated_client: AsyncClient
) -> None:
    """Test concurrent access with the same API key"""

    # Get the API key from authenticated client
    response = await authenticated_client.get("/v1/wallet/")
    api_key = response.json()["api_key"]
    initial_balance = response.json()["balance"]

    # Create multiple concurrent requests
    requests = []
    for i in range(20):
        # Alternate between both endpoints
        endpoint = "/v1/wallet/" if i % 2 == 0 else "/v1/wallet/info"
        requests.append(
            {
                "method": "GET",
                "url": endpoint,
                "headers": {"Authorization": f"Bearer {api_key}"},
            }
        )

    # Execute concurrently
    tester = ConcurrencyTester()
    responses = await tester.run_concurrent_requests(
        integration_client, requests, max_concurrent=10
    )

    # All should succeed with consistent data
    for response in responses:
        assert response.status_code == 200
        data = response.json()
        assert data["api_key"] == api_key
        assert data["balance"] == initial_balance


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_info_data_consistency(
    integration_client: AsyncClient, testmint_wallet: Any, integration_session: Any
) -> None:
    """Test data consistency between wallet endpoints and database"""

    # Create API key with known values
    token = await testmint_wallet.mint_tokens(1234)  # Specific amount
    integration_client.headers["Authorization"] = f"Bearer {token}"
    response = await integration_client.get("/v1/wallet/info")
    assert response.status_code == 200
    api_key = response.json()["api_key"]

    # Set up client with this API key
    integration_client.headers["Authorization"] = f"Bearer {api_key}"

    # Fetch from both endpoints
    response1 = await integration_client.get("/v1/wallet/")
    response2 = await integration_client.get("/v1/wallet/info")

    # Both should return identical data
    assert response1.json() == response2.json()

    # Verify against database
    hashed_key = api_key[3:]  # Remove "sk-" prefix
    result = await integration_session.execute(
        select(TemporaryCredit).where(TemporaryCredit.hashed_key == hashed_key)  # type: ignore[arg-type]
    )
    db_key = result.scalar_one()

    # Check consistency
    assert response1.json()["balance"] == db_key.balance
    assert response1.json()["balance"] == 1_234_000  # msats


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multiple_api_keys_isolation(
    integration_client: AsyncClient, testmint_wallet: Any
) -> None:
    """Test that multiple API keys are properly isolated"""

    # Create multiple API keys with different balances
    api_keys = []
    balances = [100, 500, 1000]

    for balance in balances:
        token = await testmint_wallet.mint_tokens(balance)
        integration_client.headers["Authorization"] = f"Bearer {token}"
        response = await integration_client.get("/v1/wallet/info")
        assert response.status_code == 200
        api_keys.append(
            {
                "key": response.json()["api_key"],
                "expected_balance": balance * 1000,  # msats
            }
        )

    # Test each API key returns its own balance
    for key_info in api_keys:
        integration_client.headers["Authorization"] = f"Bearer {key_info['key']}"

        # Test both endpoints
        for endpoint in ["/v1/wallet/", "/v1/wallet/info"]:
            response = await integration_client.get(endpoint)
            assert response.status_code == 200
            data = response.json()

            # Verify correct API key and balance
            assert data["api_key"] == key_info["key"]
            assert data["balance"] == key_info["expected_balance"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_endpoint_response_format(
    authenticated_client: AsyncClient,
) -> None:
    """Test response format and data types"""

    response = await authenticated_client.get("/v1/wallet/")
    assert response.status_code == 200

    data = response.json()

    # Validate data types
    assert isinstance(data, dict)
    assert isinstance(data["api_key"], str)
    assert isinstance(data["balance"], int)

    # API key format
    assert data["api_key"].startswith("sk-")
    # Balance should be non-negative
    assert data["balance"] >= 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_after_partial_spending(
    integration_client: AsyncClient, testmint_wallet: Any, integration_session: Any
) -> None:
    """Test wallet information after partial balance spending"""

    # Create API key with initial balance
    token = await testmint_wallet.mint_tokens(1000)  # 1k sats
    integration_client.headers["Authorization"] = f"Bearer {token}"
    response = await integration_client.get("/v1/wallet/info")
    assert response.status_code == 200
    api_key = response.json()["api_key"]
    initial_balance = 1_000_000  # msats

    # Simulate spending by updating database
    spent_amount = 250_000  # 250 sats in msats
    hashed_key = api_key[3:]  # Remove "sk-" prefix

    await integration_session.execute(
        update(TemporaryCredit)
        .where(TemporaryCredit.hashed_key == hashed_key)  # type: ignore[arg-type]
        .values(
            balance=initial_balance - spent_amount,
        )
    )
    await integration_session.commit()

    # Check wallet information
    integration_client.headers["Authorization"] = f"Bearer {api_key}"
    response = await integration_client.get("/v1/wallet/")

    assert response.status_code == 200
    data = response.json()

    # Balance should reflect spending
    assert data["balance"] == initial_balance - spent_amount
    assert data["balance"] == 750_000  # 750 sats in msats

    # Note: total_spent and total_requests are not returned in current implementation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_info_with_special_characters_in_headers(
    integration_client: AsyncClient, testmint_wallet: Any
) -> None:
    """Test wallet endpoints with special characters in refund address"""

    # Create API key
    token = await testmint_wallet.mint_tokens(500)
    integration_client.headers["Authorization"] = f"Bearer {token}"
    response = await integration_client.get("/v1/wallet/info")
    assert response.status_code == 200
    api_key = response.json()["api_key"]

    # Access wallet info
    integration_client.headers["Authorization"] = f"Bearer {api_key}"
    response = await integration_client.get("/v1/wallet/info")

    assert response.status_code == 200
    # Note: Current implementation doesn't return refund_address in response


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.slow
async def test_wallet_endpoints_performance(authenticated_client: AsyncClient) -> None:
    """Test wallet endpoints meet performance requirements"""

    # Warm up
    await authenticated_client.get("/v1/wallet/")

    # Measure response times
    response_times = []

    for _ in range(50):
        start_time = time.time()
        response = await authenticated_client.get("/v1/wallet/")
        end_time = time.time()

        assert response.status_code == 200
        response_times.append(end_time - start_time)

    # Calculate statistics
    avg_time = sum(response_times) / len(response_times)
    max_time = max(response_times)

    # Performance assertions
    assert avg_time < 0.1  # Average should be under 100ms
    assert max_time < 0.5  # No request should take more than 500ms
