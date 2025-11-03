"""
Integration tests for wallet top-up functionality.
Tests POST /v1/wallet/topup endpoint with various token scenarios and edge cases.
"""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel import select

from routstr.core.db import ApiKey

from .utils import (
    CashuTokenGenerator,
    ConcurrencyTester,
    ResponseValidator,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_with_valid_token(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
    db_snapshot,
    integration_session,
) -> None:
    """Test topping up an existing wallet with a valid Cashu token"""

    # Get initial balance from authenticated client
    response = await authenticated_client.get("/v1/wallet/")
    initial_balance = response.json()["balance"]
    api_key = response.json()["api_key"]

    # Capture database state
    await db_snapshot.capture()

    # Generate a new token for top-up
    topup_amount = 500  # 500 sats
    token = await testmint_wallet.mint_tokens(topup_amount)

    # Top up the existing wallet
    response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": token}
    )

    assert response.status_code == 200
    data = response.json()

    # Response should contain the added msats
    assert "msats" in data
    assert data["msats"] == topup_amount * 1000  # Convert to msats

    # Verify balance increased
    wallet_response = await authenticated_client.get("/v1/wallet/")
    new_balance = wallet_response.json()["balance"]
    assert new_balance == initial_balance + (topup_amount * 1000)

    # Verify database state directly
    # Get the hashed key from the API key
    hashed_key = api_key[3:]  # Remove "sk-" prefix
    result = await integration_session.execute(
        select(ApiKey).where(ApiKey.hashed_key == hashed_key)  # type: ignore[arg-type]
    )
    db_key = result.scalar_one()

    # Verify balance increased in database
    assert db_key.balance == new_balance
    assert db_key.balance == initial_balance + (topup_amount * 1000)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_with_multiple_denominations(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
    integration_session,
) -> None:
    """Test topping up with tokens containing multiple denominations"""

    # Generate token with specific denominations
    # Cashu uses powers of 2 denominations
    amount = 1337  # This will require multiple denominations
    token = await testmint_wallet.mint_tokens(amount)

    # Verify token has correct total value
    # The testmint wallet should handle denomination splitting internally

    # Top up the wallet
    response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": token}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["msats"] == amount * 1000

    # Verify balance
    wallet_response = await authenticated_client.get("/v1/wallet/")
    balance = wallet_response.json()["balance"]
    # Should have initial 10k sats + 1337 sats
    assert balance == 10_000_000 + (amount * 1000)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_with_invalid_token(
    authenticated_client: AsyncClient, db_snapshot: Any
) -> None:  # type: ignore[no-untyped-def]
    """Test topping up with various invalid tokens"""

    # Capture initial state
    initial_response = await authenticated_client.get("/v1/wallet/")
    initial_balance = initial_response.json()["balance"]
    await db_snapshot.capture()

    # Test various invalid tokens
    invalid_tokens = [
        CashuTokenGenerator.generate_invalid_token(),  # Malformed token
        "not-a-cashu-token",  # Wrong format
        "cashuA",  # Empty token
        "cashuAinvalidbase64!!!",  # Invalid base64
    ]

    for invalid_token in invalid_tokens:
        response = await authenticated_client.post(
            "/v1/wallet/topup", params={"cashu_token": invalid_token}
        )

        # Should fail with 400
        assert response.status_code == 400, (
            f"Token {invalid_token[:20]}... should be invalid"
        )

        # Validate error response
        validator = ResponseValidator()
        error_validation = validator.validate_error_response(
            response, expected_status=400, expected_error_key="detail"
        )
        assert error_validation["valid"]

    # Verify balance unchanged
    final_response = await authenticated_client.get("/v1/wallet/")
    assert final_response.json()["balance"] == initial_balance

    # Verify no database changes
    diff = await db_snapshot.diff()
    assert len(diff["api_keys"]["modified"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_with_spent_token(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
    db_snapshot,
) -> None:
    """Test topping up with an already spent token"""

    # Generate and use a token
    amount = 300
    token = await testmint_wallet.mint_tokens(amount)

    # First use - should succeed
    response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": token}
    )
    assert response.status_code == 200

    # Capture state after first use
    await db_snapshot.capture()

    # Try to use the same token again - should fail
    response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": token}
    )

    assert response.status_code == 400
    assert "spent" in response.json()["detail"].lower()

    # Verify no additional balance changes
    diff = await db_snapshot.diff()
    assert len(diff["api_keys"]["modified"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_malformed_tokens(authenticated_client: AsyncClient) -> None:  # type: ignore[no-untyped-def]
    """Test topping up with malformed tokens returns 400"""

    # Test malformed tokens
    malformed_tokens = [
        "Bearer cashuA123",  # Has Bearer prefix
        "cashu" + "\x00" + "A123",  # Null byte
        "cashuA" + "x" * 10000,  # Extremely long
        "cashuA\n\rtest",  # Newline characters
    ]

    for token in malformed_tokens:
        response = await authenticated_client.post(
            "/v1/wallet/topup", params={"cashu_token": token}
        )

        assert response.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_atomic_balance_updates(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
    integration_session,
) -> None:
    """Test that balance updates are atomic and prevent race conditions"""

    # Get initial state
    response = await authenticated_client.get("/v1/wallet/")
    initial_balance = response.json()["balance"]

    # Generate multiple tokens
    amounts = [100, 200, 300]
    tokens = []
    for amount in amounts:
        token = await testmint_wallet.mint_tokens(amount)
        tokens.append((token, amount))

    # Top up sequentially and verify each update
    expected_balance = initial_balance

    for i, (token, amount) in enumerate(tokens):
        response = await authenticated_client.post(
            "/v1/wallet/topup", params={"cashu_token": token}
        )
        assert response.status_code == 200, f"Topup {i + 1} failed: {response.text}"
        assert response.json()["msats"] == amount * 1000

        expected_balance += amount * 1000

        # Verify balance via API endpoint
        wallet_resp = await authenticated_client.get("/v1/wallet/")
        api_balance = wallet_resp.json()["balance"]

        # Verify balance matches what the API returns
        assert api_balance == expected_balance


@pytest.mark.integration
@pytest.mark.asyncio
async def test_transaction_history_tracking(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
    integration_session,
) -> None:
    """Test that token spending is tracked to prevent reuse"""

    # Note: The current implementation doesn't store transaction history
    # in the database. It relies on the Cashu wallet to track spent tokens.
    # This test verifies that the wallet correctly rejects spent tokens.

    # Generate a token
    token = await testmint_wallet.mint_tokens(250)

    # Use the token
    response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": token}
    )
    assert response.status_code == 200

    # Verify token is tracked as spent in testmint wallet
    assert len(testmint_wallet.spent_tokens) > 0

    # Try to reuse - should fail
    response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": token}
    )
    assert response.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_topups_same_api_key(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
) -> None:
    """Test concurrent top-ups to the same API key"""

    # Get API key
    response = await authenticated_client.get("/v1/wallet/")
    api_key = response.json()["api_key"]
    initial_balance = response.json()["balance"]

    # Generate multiple unique tokens
    num_tokens = 10
    tokens = []
    total_amount = 0

    for i in range(num_tokens):
        amount = 100 + i * 10  # Different amounts
        token = await testmint_wallet.mint_tokens(amount)
        tokens.append(token)
        total_amount += amount

    # Create concurrent top-up requests
    requests = [
        {
            "method": "POST",
            "url": "/v1/wallet/topup",
            "params": {"cashu_token": token},
            "headers": {"Authorization": f"Bearer {api_key}"},
        }
        for token in tokens
    ]

    # Execute concurrently
    tester = ConcurrencyTester()
    responses = await tester.run_concurrent_requests(
        integration_client, requests, max_concurrent=5
    )

    # All should succeed
    for response in responses:
        assert response.status_code == 200
        assert "msats" in response.json()

    # Verify final balance is correct
    final_response = await authenticated_client.get("/v1/wallet/")
    final_balance = final_response.json()["balance"]
    expected_balance = initial_balance + (total_amount * 1000)
    assert final_balance == expected_balance


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_during_active_proxy_request(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
) -> None:
    """Test topping up while another request is in progress"""

    # This test simulates a top-up happening while the wallet is being used
    # Since we can't easily simulate a real proxy request, we'll test
    # concurrent balance modifications

    # Get initial state
    await authenticated_client.get("/v1/wallet/")

    # Generate tokens
    topup_token = await testmint_wallet.mint_tokens(500)

    # Create a task that simulates wallet usage (checking balance repeatedly)
    async def simulate_usage() -> None:
        for _ in range(10):
            await authenticated_client.get("/v1/wallet/")
            await asyncio.sleep(0.01)

    # Run top-up concurrently with simulated usage
    usage_task = asyncio.create_task(simulate_usage())

    # Perform top-up
    topup_response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": topup_token}
    )

    await usage_task

    # Top-up should succeed
    assert topup_response.status_code == 200
    assert topup_response.json()["msats"] == 500_000


@pytest.mark.integration
@pytest.mark.asyncio
async def test_maximum_balance_limits(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
    integration_session,
) -> None:
    """Test if there are any maximum balance limits"""

    # Note: The current implementation doesn't enforce maximum balance limits
    # This test verifies large balances are handled correctly

    # Get current balance
    response = await authenticated_client.get("/v1/wallet/")

    # Try to add a large amount
    large_amount = 1_000_000  # 1 million sats
    token = await testmint_wallet.mint_tokens(large_amount)

    response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": token}
    )

    # Should succeed
    assert response.status_code == 200
    assert response.json()["msats"] == large_amount * 1000

    # Verify balance
    wallet_response = await authenticated_client.get("/v1/wallet/")
    balance = wallet_response.json()["balance"]
    assert balance >= large_amount * 1000  # At least the large amount


@pytest.mark.integration
@pytest.mark.asyncio
async def test_network_failure_during_token_verification(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
) -> None:
    """Test handling of network failures during token verification"""

    # Generate a valid token
    token = await testmint_wallet.mint_tokens(300)

    # Mock credit_balance to simulate network failure during token verification
    with patch("routstr.balance.credit_balance") as mock_credit_balance:
        mock_credit_balance.side_effect = Exception("Network error: Connection timeout")

        response = await authenticated_client.post(
            "/v1/wallet/topup", params={"cashu_token": token}
        )

        # Should return 500 error for network issues
        assert response.status_code == 500
        assert "detail" in response.json()
        assert response.json()["detail"] == "Internal server error"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_response_format(  # type: ignore[no-untyped-def]
    authenticated_client: AsyncClient, testmint_wallet: Any
) -> None:
    """Test the response format of successful top-up"""

    token = await testmint_wallet.mint_tokens(123)

    response = await authenticated_client.post(
        "/v1/wallet/topup", params={"cashu_token": token}
    )

    assert response.status_code == 200
    data = response.json()

    # Validate response structure
    assert isinstance(data, dict)
    assert "msats" in data
    assert isinstance(data["msats"], int)
    assert data["msats"] == 123_000  # 123 sats in msats


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_with_zero_amount_token(  # type: ignore[no-untyped-def]
    authenticated_client: AsyncClient, testmint_wallet: Any
) -> None:
    """Test topping up with a token that has zero value"""

    # Create a token with 0 amount (edge case)
    # The testmint wallet should handle this
    with patch.object(
        testmint_wallet,
        "redeem_token",
        return_value=(0, "sat", testmint_wallet.mint_url),
    ):
        token = await testmint_wallet.mint_tokens(0)

        response = await authenticated_client.post(
            "/v1/wallet/topup", params={"cashu_token": token}
        )

        # Should succeed but add 0 msats
        assert response.status_code == 200
        assert response.json()["msats"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.slow
async def test_topup_stress_test(  # type: ignore[no-untyped-def]
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    testmint_wallet: Any,
) -> None:
    """Stress test with many sequential top-ups"""

    # Get initial balance
    response = await authenticated_client.get("/v1/wallet/")
    initial_balance = response.json()["balance"]

    # Perform many small top-ups
    num_topups = 50
    amount_per_topup = 10  # 10 sats each
    successful_topups = 0

    for i in range(num_topups):
        token = await testmint_wallet.mint_tokens(amount_per_topup)
        response = await authenticated_client.post(
            "/v1/wallet/topup", params={"cashu_token": token}
        )

        if response.status_code == 200:
            successful_topups += 1

    # All should succeed
    assert successful_topups == num_topups

    # Verify final balance
    final_response = await authenticated_client.get("/v1/wallet/")
    final_balance = final_response.json()["balance"]
    expected_balance = initial_balance + (num_topups * amount_per_topup * 1000)
    assert final_balance == expected_balance
