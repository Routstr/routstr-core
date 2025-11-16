"""Integration tests for admin upstream provider management"""

import os
import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

os.environ["ADMIN_PASSWORD"] = "test-admin-password-123"


@pytest.fixture
async def admin_token(integration_client: AsyncClient) -> str:
    """Get admin authentication token"""
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": "test-admin-password-123"},
    )
    assert response.status_code == 200
    return response.json()["token"]


@pytest.fixture
def admin_headers(admin_token: str) -> dict[str, str]:
    """Get admin authentication headers"""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.mark.asyncio
async def test_list_upstream_providers(
    integration_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Test listing upstream providers"""
    response = await integration_client.get(
        "/admin/api/upstream-providers", headers=admin_headers
    )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_create_upstream_provider(
    integration_client: AsyncClient,
    admin_headers: dict[str, str],
    integration_session: AsyncSession,
) -> None:
    """Test creating an upstream provider"""
    provider_data = {
        "provider_type": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "test-api-key-123",
        "enabled": True,
        "provider_fee": 1.01,
    }

    response = await integration_client.post(
        "/admin/api/upstream-providers",
        json=provider_data,
        headers=admin_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["provider_type"] == "openai"
    assert data["base_url"] == "https://api.openai.com/v1"
    assert data["enabled"] is True


@pytest.mark.asyncio
async def test_create_upstream_provider_validation(
    integration_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Test provider creation validation"""
    invalid_data = {
        "provider_type": "",
        "base_url": "",
        "api_key": "",
    }

    response = await integration_client.post(
        "/admin/api/upstream-providers",
        json=invalid_data,
        headers=admin_headers,
    )

    assert response.status_code in [400, 422]


@pytest.mark.asyncio
async def test_update_upstream_provider(
    integration_client: AsyncClient,
    admin_headers: dict[str, str],
    integration_session: AsyncSession,
) -> None:
    """Test updating an upstream provider"""
    from routstr.core.db import UpstreamProviderRow

    provider = UpstreamProviderRow(
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        enabled=True,
        provider_fee=1.01,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)

    assert provider.id is not None

    update_data = {
        "enabled": False,
        "provider_fee": 1.02,
    }

    response = await integration_client.patch(
        f"/admin/api/upstream-providers/{provider.id}",
        json=update_data,
        headers=admin_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["provider_fee"] == 1.02


@pytest.mark.asyncio
async def test_delete_upstream_provider(
    integration_client: AsyncClient,
    admin_headers: dict[str, str],
    integration_session: AsyncSession,
) -> None:
    """Test deleting an upstream provider"""
    from routstr.core.db import UpstreamProviderRow

    provider = UpstreamProviderRow(
        provider_type="test",
        base_url="https://test.example.com",
        api_key="test-key",
        enabled=True,
        provider_fee=1.01,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)

    assert provider.id is not None

    response = await integration_client.delete(
        f"/admin/api/upstream-providers/{provider.id}",
        headers=admin_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_get_provider_types(
    integration_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Test getting available provider types"""
    response = await integration_client.get(
        "/admin/api/provider-types", headers=admin_headers
    )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0


@pytest.mark.asyncio
async def test_test_provider_connection(
    integration_client: AsyncClient,
    admin_headers: dict[str, str],
    integration_session: AsyncSession,
) -> None:
    """Test testing provider connection"""
    from routstr.core.db import UpstreamProviderRow

    provider = UpstreamProviderRow(
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        enabled=True,
        provider_fee=1.01,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)

    assert provider.id is not None

    response = await integration_client.get(
        f"/admin/api/upstream-providers/{provider.id}/test",
        headers=admin_headers,
    )

    assert response.status_code in [200, 500, 502]
