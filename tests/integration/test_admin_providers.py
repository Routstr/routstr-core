"""Integration tests for admin upstream provider management."""

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.core.settings import SettingsService


@pytest.fixture
async def admin_token(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> str:
    """Fixture to get an admin authentication token."""
    test_password = "test_admin_password_123"
    await SettingsService.update({"admin_password": test_password}, integration_session)
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    return response.json()["token"]


@pytest.mark.asyncio
async def test_list_upstream_providers_empty(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test listing upstream providers when none exist."""
    result = await integration_session.exec(select(UpstreamProviderRow))
    for provider in result.all():
        await integration_session.delete(provider)
    await integration_session.commit()
    
    response = await integration_client.get(
        "/admin/api/upstream-providers",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    providers = response.json()
    assert isinstance(providers, list)


@pytest.mark.asyncio
async def test_create_upstream_provider(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test creating a new upstream provider."""
    response = await integration_client.post(
        "/admin/api/upstream-providers",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "provider_type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "test_api_key_123",
            "enabled": True,
            "provider_fee": 1.05,
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["provider_type"] == "openai"
    assert data["base_url"] == "https://api.openai.com/v1"
    assert data["api_key"] == "[REDACTED]"
    assert data["enabled"] is True
    assert data["provider_fee"] == 1.05
    assert "id" in data


@pytest.mark.asyncio
async def test_create_upstream_provider_duplicate_base_url(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test that duplicate base URLs are rejected."""
    base_url = "https://api.test-provider.com/v1"
    
    existing_provider = UpstreamProviderRow(
        provider_type="openai",
        base_url=base_url,
        api_key="existing_key",
        enabled=True,
        provider_fee=1.0,
    )
    integration_session.add(existing_provider)
    await integration_session.commit()
    
    response = await integration_client.post(
        "/admin/api/upstream-providers",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "provider_type": "openai",
            "base_url": base_url,
            "api_key": "new_key",
            "enabled": True,
            "provider_fee": 1.0,
        },
    )
    
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_single_upstream_provider(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test getting a single upstream provider by ID."""
    provider = UpstreamProviderRow(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key="test_key",
        enabled=True,
        provider_fee=1.02,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    
    response = await integration_client.get(
        f"/admin/api/upstream-providers/{provider.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == provider.id
    assert data["provider_type"] == "anthropic"
    assert data["api_key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_get_nonexistent_upstream_provider(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test getting a provider that doesn't exist."""
    response = await integration_client.get(
        "/admin/api/upstream-providers/99999",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_upstream_provider(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test updating an existing upstream provider."""
    provider = UpstreamProviderRow(
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        api_key="old_key",
        enabled=True,
        provider_fee=1.0,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    
    response = await integration_client.patch(
        f"/admin/api/upstream-providers/{provider.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "api_key": "new_key",
            "enabled": False,
            "provider_fee": 1.10,
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["provider_fee"] == 1.10
    assert data["api_key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_update_nonexistent_upstream_provider(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test updating a provider that doesn't exist."""
    response = await integration_client.patch(
        "/admin/api/upstream-providers/99999",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"enabled": False},
    )
    
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_upstream_provider(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test deleting an upstream provider."""
    provider = UpstreamProviderRow(
        provider_type="openai",
        base_url="https://api.delete-test.com/v1",
        api_key="test_key",
        enabled=True,
        provider_fee=1.0,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    provider_id = provider.id
    
    response = await integration_client.delete(
        f"/admin/api/upstream-providers/{provider_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["deleted_id"] == provider_id
    
    deleted_provider = await integration_session.get(UpstreamProviderRow, provider_id)
    assert deleted_provider is None


@pytest.mark.asyncio
async def test_delete_nonexistent_upstream_provider(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test deleting a provider that doesn't exist."""
    response = await integration_client.delete(
        "/admin/api/upstream-providers/99999",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_provider_cascades_to_models(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test that deleting a provider also deletes associated models."""
    provider = UpstreamProviderRow(
        provider_type="openai",
        base_url="https://api.cascade-test.com/v1",
        api_key="test_key",
        enabled=True,
        provider_fee=1.0,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    
    model = ModelRow(
        id="test-model",
        upstream_provider_id=provider.id,
        name="test-model",
        created=0,
        description="Test model",
        context_length=4096,
        architecture="gpt",
        pricing='{"input": 100, "output": 200}',
        enabled=True,
    )
    integration_session.add(model)
    await integration_session.commit()
    
    response = await integration_client.delete(
        f"/admin/api/upstream-providers/{provider.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    
    deleted_model = await integration_session.get(
        ModelRow, {"id": "test-model", "upstream_provider_id": provider.id}
    )
    assert deleted_model is None


@pytest.mark.asyncio
async def test_list_provider_types(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test listing available provider types."""
    response = await integration_client.get(
        "/admin/api/provider-types",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    types = response.json()
    assert isinstance(types, list)
    assert len(types) > 0
    
    for provider_type in types:
        assert "provider_type" in provider_type
        assert "display_name" in provider_type


@pytest.mark.asyncio
async def test_get_provider_models(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test getting models for a specific provider."""
    provider = UpstreamProviderRow(
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        api_key="test_key",
        enabled=True,
        provider_fee=1.0,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    
    response = await integration_client.get(
        f"/admin/api/upstream-providers/{provider.id}/models",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "provider" in data
    assert "db_models" in data
    assert "remote_models" in data
    assert data["provider"]["id"] == provider.id


@pytest.mark.asyncio
async def test_upstream_provider_requires_authentication(
    integration_client: AsyncClient,
) -> None:
    """Test that all provider endpoints require authentication."""
    endpoints = [
        ("GET", "/admin/api/upstream-providers"),
        ("POST", "/admin/api/upstream-providers"),
        ("GET", "/admin/api/upstream-providers/1"),
        ("PATCH", "/admin/api/upstream-providers/1"),
        ("DELETE", "/admin/api/upstream-providers/1"),
        ("GET", "/admin/api/provider-types"),
    ]
    
    for method, endpoint in endpoints:
        if method == "GET":
            response = await integration_client.get(endpoint)
        elif method == "POST":
            response = await integration_client.post(endpoint, json={})
        elif method == "PATCH":
            response = await integration_client.patch(endpoint, json={})
        elif method == "DELETE":
            response = await integration_client.delete(endpoint)
        
        assert response.status_code == 403, f"{method} {endpoint} should require auth"


@pytest.mark.asyncio
async def test_create_provider_with_api_version(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test creating a provider with api_version (for Azure OpenAI)."""
    response = await integration_client.post(
        "/admin/api/upstream-providers",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "provider_type": "azure",
            "base_url": "https://test-azure.openai.azure.com",
            "api_key": "test_key",
            "api_version": "2024-02-15-preview",
            "enabled": True,
            "provider_fee": 1.0,
        },
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["api_version"] == "2024-02-15-preview"


@pytest.mark.asyncio
async def test_list_providers_returns_all_fields(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test that listing providers returns all expected fields."""
    provider = UpstreamProviderRow(
        provider_type="openai",
        base_url="https://api.test.com/v1",
        api_key="test_key",
        api_version="v1",
        enabled=True,
        provider_fee=1.03,
    )
    integration_session.add(provider)
    await integration_session.commit()
    
    response = await integration_client.get(
        "/admin/api/upstream-providers",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    providers = response.json()
    assert len(providers) > 0
    
    test_provider = next(
        (p for p in providers if p["base_url"] == "https://api.test.com/v1"), None
    )
    assert test_provider is not None
    assert test_provider["api_key"] == "[REDACTED]"
    assert test_provider["provider_fee"] == 1.03
    assert test_provider["enabled"] is True
