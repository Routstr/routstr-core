import pytest
import pytest_asyncio
import hashlib
import uuid
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient
from router.db import ApiKey, AsyncSession


def hash_api_key(api_key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()


@pytest_asyncio.fixture
async def test_api_key(test_session: AsyncSession) -> ApiKey:
    """Create a test API key in the database."""
    # Use unique key for each test
    unique_id = str(uuid.uuid4())[:8]
    api_key = f"test-api-key-{unique_id}"

    key = ApiKey(
        hashed_key=api_key,
        balance=1000000,  # 1000 sats in msats
        refund_address="test@lightning.address",
        total_spent=0,
        total_requests=0,
    )

    test_session.add(key)
    await test_session.commit()
    await test_session.refresh(key)

    return key


@pytest.mark.asyncio
async def test_account_info_with_valid_key(
    async_client: AsyncClient, test_api_key: ApiKey
):
    """Test getting account info with a valid API key."""
    response = await async_client.get(
        "/v1/wallet/info",
        headers={"Authorization": f"Bearer sk-{test_api_key.hashed_key}"},
    )

    assert response.status_code == 200
    data = response.json()

    assert data["api_key"] == f"sk-{test_api_key.hashed_key}"
    assert data["balance"] == 1000000


@pytest.mark.asyncio
async def test_account_info_without_auth(async_client: AsyncClient):
    """Test that account info requires authentication."""
    response = await async_client.get("/v1/wallet/info")

    assert response.status_code == 422  # Missing required header


@pytest.mark.asyncio
async def test_account_info_with_invalid_key(async_client: AsyncClient):
    """Test account info with an invalid API key."""
    response = await async_client.get(
        "/v1/wallet/info",
        headers={"Authorization": "Bearer invalid-key"},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refund_balance_with_address(
    async_client: AsyncClient, test_api_key: ApiKey, test_session: AsyncSession
):
    """Test refunding balance when refund address is set."""
    # Need to patch the refund_balance at the module level to intercept the call
    with patch("router.account.refund_balance", new_callable=AsyncMock) as mock_refund:
        mock_refund.return_value = 1000000

        response = await async_client.post(
            "/v1/wallet/refund",
            headers={"Authorization": f"Bearer sk-{test_api_key.hashed_key}"},
        )

        assert response.status_code == 200
        data = response.json()

        assert data["recipient"] == "test@lightning.address"
        assert data["msats"] == 1000000

        # Verify balance was zeroed
        await test_session.refresh(test_api_key)
        assert test_api_key.balance == 0

        # Verify refund_balance was called
        mock_refund.assert_called_once()


@pytest.mark.asyncio
async def test_refund_balance_without_address(
    async_client: AsyncClient, test_session: AsyncSession
):
    """Test refunding balance when no refund address is set."""
    # Create key without refund address - with unique ID
    unique_id = str(uuid.uuid4())[:8]
    api_key = f"test-key-no-refund-{unique_id}"

    key = ApiKey(
        hashed_key=api_key,
        balance=500000,
        refund_address=None,
        total_spent=0,
        total_requests=0,
    )

    test_session.add(key)
    await test_session.commit()

    # Mock the WALLET instance at the router.account module level
    with patch("router.account.WALLET") as mock_wallet:
        mock_wallet.send = AsyncMock(return_value="cashuBqQSEQ...")

        response = await async_client.post(
            "/v1/wallet/refund", headers={"Authorization": f"Bearer sk-{api_key}"}
        )

        assert response.status_code == 200
        data = response.json()

        assert data["recipient"] is None
        assert data["msats"] == 500000
        assert data["token"] == "cashuBqQSEQ..."

        # Verify wallet.send was called with the correct amount (msats converted to sats)
        mock_wallet.send.assert_called_once_with(500)


@pytest.mark.asyncio
async def test_topup_balance_endpoint(
    async_client: AsyncClient, test_api_key: ApiKey, test_session: AsyncSession
):
    """Test topping up balance with a cashu token."""
    # Mock at the router.account module level to intercept the import
    with patch("router.account.credit_balance", new_callable=AsyncMock) as mock_credit:
        mock_credit.return_value = {"msats": 500000}

        response = await async_client.post(
            "/v1/wallet/topup?cashu_token=cashuBqQSEQ...",
            headers={"Authorization": f"Bearer sk-{test_api_key.hashed_key}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data == {"msats": 500000}

        # Verify credit_balance was called
        mock_credit.assert_called_once()


@pytest.mark.asyncio
async def test_topup_balance_requires_cashu_token(
    async_client: AsyncClient, test_api_key: ApiKey
):
    """Test that topup endpoint requires a cashu token."""
    response = await async_client.post(
        "/v1/wallet/topup",
        headers={"Authorization": f"Bearer sk-{test_api_key.hashed_key}"},
        json={},
    )

    assert response.status_code == 422  # Missing required field


@pytest.mark.asyncio
async def test_account_with_cashu_token(
    async_client: AsyncClient, test_session: AsyncSession
):
    """Test authentication with a cashu token creates a new account."""
    cashu_token = "cashuBqQSEQ123456"

    async def mock_credit_balance(
        token: str, key: ApiKey, session: AsyncSession
    ) -> int:
        """Mock credit_balance function that simulates adding balance and committing."""
        amount = 5000000  # 5000 sats in msats
        key.balance += amount
        session.add(key)
        await session.commit()
        return amount

    with patch(
        "router.cashu.credit_balance",
        new_callable=AsyncMock,
        side_effect=mock_credit_balance,
    ):
        response = await async_client.get(
            "/v1/wallet/info",
            headers={"Authorization": f"Bearer {cashu_token}"},
        )

        assert response.status_code == 200
        data = response.json()

        # Check that a new key was created with the hashed token
        assert data["api_key"].startswith("sk-")
        assert data["balance"] >= 0  # Balance should be set after credit_balance
