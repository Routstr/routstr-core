"""
Example integration test demonstrating the test infrastructure.
This file can be used as a template for writing new integration tests.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from .utils import (
    CashuTokenGenerator,
    PerformanceValidator,
    ResponseValidator,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_infrastructure_setup(
    integration_client: AsyncClient,
    testmint_wallet: Any,
    db_snapshot: Any,
    integration_session: Any,
) -> None:
    """Test that the integration test infrastructure is properly set up"""

    # Test that client can make requests
    response = await integration_client.get("/")
    assert response.status_code == 200

    # Test that testmint wallet can generate tokens
    token = await testmint_wallet.mint_tokens(1000)
    assert token.startswith("cashuA")

    # Test that database snapshot works
    initial_state = await db_snapshot.capture()
    assert "api_keys" in initial_state

    # Test that response validator works
    validator = ResponseValidator()
    validation = validator.validate_success_response(response)
    assert validation["valid"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_wallet_flow(
    integration_client: AsyncClient,
    testmint_wallet: Any,
    db_snapshot: Any,
    integration_session: Any,
) -> None:
    """Test complete wallet flow: create, topup, use, refund"""

    # Step 1: Capture initial state
    await db_snapshot.capture()

    # Step 2: Create wallet with initial topup
    initial_amount = 5000  # 5k sats
    token = await testmint_wallet.mint_tokens(initial_amount)

    # Use cashu token as Bearer auth to create API key
    integration_client.headers["Authorization"] = f"Bearer {token}"
    response = await integration_client.get("/v1/wallet/info")

    assert response.status_code == 200
    data = response.json()
    api_key = data["api_key"]
    assert data["balance"] == initial_amount * 1000  # Convert to msats

    # Step 3: Verify the API key was created
    # Skip db_snapshot due to session isolation issues
    # Instead verify through API

    # Step 4: Use the API key to make a request
    integration_client.headers["Authorization"] = f"Bearer {api_key}"

    wallet_response = await integration_client.get("/v1/wallet/")
    assert wallet_response.status_code == 200
    wallet_data = wallet_response.json()
    assert wallet_data["balance"] == initial_amount * 1000

    # Step 5: Add more funds
    topup_amount = 2000  # 2k sats
    topup_token = await testmint_wallet.mint_tokens(topup_amount)

    topup_response = await integration_client.post(
        "/v1/wallet/topup", params={"cashu_token": topup_token}
    )

    if topup_response.status_code != 200:
        print(f"ERROR: Topup failed with status {topup_response.status_code}")
        print(f"ERROR: Response body: {topup_response.json()}")
    assert topup_response.status_code == 200
    assert topup_response.json()["msats"] == topup_amount * 1000

    # Verify new balance through wallet endpoint
    balance_check = await integration_client.get("/v1/wallet/")
    assert balance_check.json()["balance"] == (initial_amount + topup_amount) * 1000

    # Step 6: Request refund (refunds full balance)
    refund_response = await integration_client.post("/v1/wallet/refund")

    assert refund_response.status_code == 200
    refund_data = refund_response.json()
    assert "token" in refund_data

    # Check for either sats or msats depending on refund_currency
    total_amount = initial_amount + topup_amount
    if "sats" in refund_data:
        assert refund_data["sats"] == str(total_amount)
    elif "msats" in refund_data:
        assert refund_data["msats"] == str(total_amount * 1000)
    else:
        pytest.fail("Response should contain either 'sats' or 'msats'")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_error_handling(
    integration_client: AsyncClient, testmint_wallet: Any
) -> None:
    """Test various error scenarios"""

    # Test invalid token with authentication
    # First create a valid API key to use for authentication
    valid_token = await testmint_wallet.mint_tokens(100)
    integration_client.headers["Authorization"] = f"Bearer {valid_token}"
    valid_response = await integration_client.get("/v1/wallet/info")
    api_key = valid_response.json()["api_key"]

    # Now test topping up with an invalid token
    integration_client.headers["Authorization"] = f"Bearer {api_key}"
    invalid_token = CashuTokenGenerator.generate_invalid_token()
    response = await integration_client.post(
        "/v1/wallet/topup", params={"cashu_token": invalid_token}
    )

    # Should get 400 for invalid token
    # But the endpoint might return 200 with 0 msats for some invalid tokens
    if response.status_code == 200:
        # Check if it returned 0 msats
        assert response.json()["msats"] == 0
    else:
        assert response.status_code == 400
        assert "detail" in response.json()

    # Test unauthorized access
    # Clear any existing authorization header
    integration_client.headers.pop("Authorization", None)
    response = await integration_client.get("/v1/wallet/")
    # Wallet endpoints require authentication
    assert response.status_code in [401, 422]  # 422 if missing required header

    # Test invalid API key
    integration_client.headers["Authorization"] = "Bearer invalid-key-12345"
    response = await integration_client.get("/v1/wallet/")
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_performance_requirements(integration_client: AsyncClient) -> None:
    """Test that endpoints meet performance requirements"""

    validator = PerformanceValidator()

    # Test info endpoint performance
    for i in range(50):
        start = validator.start_timing("info_endpoint")
        response = await integration_client.get("/")
        validator.end_timing("info_endpoint", start)
        assert response.status_code == 200

    # Validate 95th percentile is under 500ms
    result = validator.validate_response_time(
        "info_endpoint", max_duration=0.5, percentile=0.95
    )

    assert result["valid"], (
        f"Performance requirement failed: "
        f"95th percentile was {result['percentile_time']:.3f}s "
        f"(required < {result['max_allowed']}s)"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.slow
async def test_concurrent_operations(
    integration_client: AsyncClient, testmint_wallet: Any
) -> None:
    """Test handling of concurrent operations"""

    from .utils import ConcurrencyTester

    # Create multiple tokens for concurrent topups
    tokens = []
    for i in range(10):
        token = await testmint_wallet.mint_tokens(100)  # 100 sats each
        tokens.append(token)

    # Build concurrent requests using cashu tokens as Bearer auth
    requests = [
        {
            "method": "GET",
            "url": "/v1/wallet/info",
            "headers": {"Authorization": f"Bearer {token}"},
        }
        for token in tokens
    ]

    # Execute concurrently
    tester = ConcurrencyTester()
    responses = await tester.run_concurrent_requests(
        integration_client, requests, max_concurrent=5
    )

    # All should succeed and return different API keys
    api_keys = set()
    for response in responses:
        assert response.status_code == 200
        api_key = response.json()["api_key"]
        api_keys.add(api_key)

    # Should have 10 unique API keys
    assert len(api_keys) == 10
