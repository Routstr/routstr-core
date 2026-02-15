import pytest
from httpx import AsyncClient
from typing import Any
from sqlmodel import select
from routstr.core.db import ApiKey


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_info_returns_child_keys(
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    integration_session: Any,
) -> None:
    """Test that GET /v1/wallet/info returns child keys for a parent key"""

    # 1. Get parent info to find its hashed_key
    response = await authenticated_client.get("/v1/wallet/info")
    assert response.status_code == 200
    parent_data = response.json()
    parent_api_key = parent_data["api_key"]

    # 2. Create child keys for this parent
    # We need to use the parent's authentication for this
    child_payload = {"count": 2, "balance_limit": 1000, "balance_limit_reset": "daily"}
    create_response = await authenticated_client.post(
        "/v1/wallet/child-key", json=child_payload
    )
    assert create_response.status_code == 200
    create_data = create_response.json()
    child_keys = create_data["api_keys"]
    assert len(child_keys) == 2

    # 3. Call /info again and check for child_keys
    info_response = await authenticated_client.get("/v1/wallet/info")
    assert info_response.status_code == 200
    info_data = info_response.json()

    assert "child_keys" in info_data
    assert len(info_data["child_keys"]) == 2

    # Verify child key details
    for ck in info_data["child_keys"]:
        assert ck["api_key"] in child_keys
        assert ck["balance_limit"] == 1000
        assert ck["balance_limit_reset"] == "daily"
        assert "total_spent" in ck
        assert "total_requests" in ck


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wallet_info_child_key_no_child_keys(
    integration_client: AsyncClient,
    authenticated_client: AsyncClient,
    integration_session: Any,
) -> None:
    """Test that GET /v1/wallet/info for a child key does NOT return child_keys"""

    # 1. Create a child key
    child_payload = {"count": 1}
    create_response = await authenticated_client.post(
        "/v1/wallet/child-key", json=child_payload
    )
    assert create_response.status_code == 200
    child_key = create_response.json()["api_keys"][0]

    # 2. Use the child key to get its info
    integration_client.headers["Authorization"] = f"Bearer {child_key}"
    info_response = await integration_client.get("/v1/wallet/info")
    assert info_response.status_code == 200
    info_data = info_response.json()

    assert info_data["is_child"] is True
    assert "child_keys" not in info_data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_account_info_root_returns_child_keys(
    authenticated_client: AsyncClient,
) -> None:
    """Test that GET / returns child keys for a parent key (root endpoint)"""

    # 1. Create a child key
    child_payload = {"count": 1}
    await authenticated_client.post("/v1/wallet/child-key", json=child_payload)

    # 2. Call root endpoint /v1/balance/
    # Note: routstr/balance.py defines router = APIRouter()
    # and it is included in balance_router with prefix /v1/balance
    # The endpoint is @router.get("/")
    response = await authenticated_client.get("/v1/balance/")
    assert response.status_code == 200
    data = response.json()

    assert "child_keys" in data
    assert len(data["child_keys"]) >= 1
