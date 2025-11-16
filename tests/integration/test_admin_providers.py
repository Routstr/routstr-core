"""Admin upstream provider management tests."""

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import UpstreamProviderRow, create_session


def get_admin_token(integration_client: AsyncClient) -> str:
    """Helper to get admin token."""
    import os
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    return response.json()["token"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_upstream_providers(
    integration_client: AsyncClient,
) -> None:
    """Test listing upstream providers."""
    token = get_admin_token(integration_client)
    
    response = await integration_client.get(
        "/admin/api/providers",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_upstream_provider(
    integration_client: AsyncClient,
) -> None:
    """Test creating an upstream provider."""
    token = get_admin_token(integration_client)
    
    provider_data = {
        "name": "Test Provider",
        "base_url": "https://api.test.com/v1",
        "api_key": "test-api-key",
        "provider_type": "openai",
        "provider_fee": 1.0,
    }
    
    response = await integration_client.post(
        "/admin/api/providers",
        headers={"Authorization": f"Bearer {token}"},
        json=provider_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == provider_data["name"]
    assert data["base_url"] == provider_data["base_url"]
    
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, data["id"])
        assert provider is not None
        assert provider.name == provider_data["name"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_upstream_provider_validation(
    integration_client: AsyncClient,
) -> None:
    """Test upstream provider creation validation."""
    token = get_admin_token(integration_client)
    
    invalid_data = {
        "name": "",
        "base_url": "not-a-url",
    }
    
    response = await integration_client.post(
        "/admin/api/providers",
        headers={"Authorization": f"Bearer {token}"},
        json=invalid_data,
    )
    
    assert response.status_code in [400, 422]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_upstream_provider(
    integration_client: AsyncClient,
) -> None:
    """Test updating an upstream provider."""
    token = get_admin_token(integration_client)
    
    provider_data = {
        "name": "Test Provider",
        "base_url": "https://api.test.com/v1",
        "api_key": "test-api-key",
        "provider_type": "openai",
        "provider_fee": 1.0,
    }
    
    response = await integration_client.post(
        "/admin/api/providers",
        headers={"Authorization": f"Bearer {token}"},
        json=provider_data,
    )
    
    provider_id = response.json()["id"]
    
    update_data = {
        "name": "Updated Provider",
        "provider_fee": 1.05,
    }
    
    response = await integration_client.put(
        f"/admin/api/providers/{provider_id}",
        headers={"Authorization": f"Bearer {token}"},
        json=update_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == update_data["name"]
    assert data["provider_fee"] == update_data["provider_fee"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_upstream_provider(
    integration_client: AsyncClient,
) -> None:
    """Test deleting an upstream provider."""
    token = get_admin_token(integration_client)
    
    provider_data = {
        "name": "Test Provider To Delete",
        "base_url": "https://api.test.com/v1",
        "api_key": "test-api-key",
        "provider_type": "openai",
        "provider_fee": 1.0,
    }
    
    response = await integration_client.post(
        "/admin/api/providers",
        headers={"Authorization": f"Bearer {token}"},
        json=provider_data,
    )
    
    provider_id = response.json()["id"]
    
    response = await integration_client.delete(
        f"/admin/api/providers/{provider_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code == 200
    
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, provider_id)
        assert provider is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_upstream_provider_in_use(
    integration_client: AsyncClient,
) -> None:
    """Test deleting a provider that is in use."""
    token = get_admin_token(integration_client)
    
    provider_data = {
        "name": "Provider In Use",
        "base_url": "https://api.test.com/v1",
        "api_key": "test-api-key",
        "provider_type": "openai",
        "provider_fee": 1.0,
    }
    
    response = await integration_client.post(
        "/admin/api/providers",
        headers={"Authorization": f"Bearer {token}"},
        json=provider_data,
    )
    
    provider_id = response.json()["id"]
    
    from routstr.core.db import ModelRow
    async with create_session() as session:
        model = ModelRow(
            id="test-model",
            upstream_provider_id=provider_id,
            name="Test Model",
            created=1234567890,
            description="Test",
            context_length=4096,
            architecture='{"modality": "text"}',
            pricing='{"prompt": 0.0, "completion": 0.0}',
            enabled=True,
        )
        session.add(model)
        await session.commit()
    
    response = await integration_client.delete(
        f"/admin/api/providers/{provider_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code in [400, 409]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_test_provider_connection(
    integration_client: AsyncClient,
) -> None:
    """Test testing provider connection."""
    token = get_admin_token(integration_client)
    
    provider_data = {
        "name": "Test Provider",
        "base_url": "https://api.test.com/v1",
        "api_key": "test-api-key",
        "provider_type": "openai",
        "provider_fee": 1.0,
    }
    
    response = await integration_client.post(
        "/admin/api/providers",
        headers={"Authorization": f"Bearer {token}"},
        json=provider_data,
    )
    
    provider_id = response.json()["id"]
    
    response = await integration_client.post(
        f"/admin/api/providers/{provider_id}/test",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code in [200, 400, 500]
    data = response.json()
    assert "ok" in data or "error" in data
