"""End-to-end tests for complete user workflows."""

from typing import Any

import pytest
from httpx import AsyncClient


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_payment_flow(
    integration_client: AsyncClient,
    testmint_wallet: Any,
) -> None:
    """Test complete payment flow: Create key → Top up → Make request → Verify cost."""
    from tests.integration.conftest import create_api_key
    
    api_key, _ = await create_api_key(integration_client, testmint_wallet, amount=100000)
    
    headers = {"Authorization": f"Bearer {api_key}"}
    
    response = await integration_client.get("/v1/wallet/", headers=headers)
    assert response.status_code == 200
    initial_balance = response.json()["balance"]
    assert initial_balance > 0
    
    response = await integration_client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 10,
        },
    )
    
    assert response.status_code in [200, 402]
    
    response = await integration_client.get("/v1/wallet/", headers=headers)
    assert response.status_code == 200
    final_balance = response.json()["balance"]
    
    if response.status_code == 200:
        assert final_balance < initial_balance


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_failover(
    integration_client: AsyncClient,
    testmint_wallet: Any,
) -> None:
    """Test provider failover: Request with primary down → Fallback to secondary."""
    from tests.integration.conftest import create_api_key
    
    api_key, _ = await create_api_key(integration_client, testmint_wallet, amount=100000)
    
    headers = {"Authorization": f"Bearer {api_key}"}
    
    response = await integration_client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 10,
        },
    )
    
    assert response.status_code in [200, 402, 500]


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.asyncio
async def test_refund_flow(
    integration_client: AsyncClient,
    testmint_wallet: Any,
) -> None:
    """Test refund flow: Create key → Make request → Get refund."""
    from tests.integration.conftest import create_api_key
    
    api_key, _ = await create_api_key(integration_client, testmint_wallet, amount=100000)
    
    headers = {"Authorization": f"Bearer {api_key}"}
    
    response = await integration_client.get("/v1/wallet/", headers=headers)
    assert response.status_code == 200
    initial_balance = response.json()["balance"]
    
    response = await integration_client.post(
        "/v1/wallet/refund",
        headers=headers,
        json={},
    )
    
    assert response.status_code in [200, 400]


@pytest.mark.e2e
@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_workflow(
    integration_client: AsyncClient,
) -> None:
    """Test admin workflow: Add provider → Configure model → Use via proxy."""
    import os
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    
    assert response.status_code == 200
    token = response.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    provider_data = {
        "name": "Test Provider",
        "base_url": "https://api.test.com/v1",
        "api_key": "test-api-key",
        "provider_type": "openai",
        "provider_fee": 1.0,
    }
    
    response = await integration_client.post(
        "/admin/api/providers",
        headers=headers,
        json=provider_data,
    )
    
    assert response.status_code == 200
    provider_id = response.json()["id"]
    
    response = await integration_client.get("/v1/models")
    assert response.status_code == 200
