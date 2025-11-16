"""Integration tests for admin model management"""

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


@pytest.fixture
async def test_provider(integration_session: AsyncSession) -> int:
    """Create a test provider for model tests"""
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
    return provider.id


@pytest.mark.asyncio
async def test_create_provider_model(
    integration_client: AsyncClient,
    admin_headers: dict[str, str],
    test_provider: int,
) -> None:
    """Test creating a model for a provider"""
    model_data = {
        "id": "test-model-123",
        "name": "Test Model",
        "description": "A test model",
        "created": 1234567890,
        "context_length": 4096,
        "architecture": {
            "modality": "text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tokenizer": "test",
        },
        "pricing": {
            "prompt": 0.001,
            "completion": 0.002,
            "request": 0.0,
            "image": 0.0,
            "web_search": 0.0,
            "internal_reasoning": 0.0,
        },
        "enabled": True,
    }

    response = await integration_client.post(
        f"/admin/api/upstream-providers/{test_provider}/models",
        json=model_data,
        headers=admin_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "test-model-123"
    assert data["name"] == "Test Model"


@pytest.mark.asyncio
async def test_update_provider_model(
    integration_client: AsyncClient,
    admin_headers: dict[str, str],
    test_provider: int,
) -> None:
    """Test updating a provider model"""
    from routstr.core.db import ModelRow

    model = ModelRow(
        id="test-model-update",
        upstream_provider_id=test_provider,
        name="Original Name",
        description="Original description",
        created=1234567890,
        context_length=4096,
        architecture='{"modality": "text"}',
        pricing='{"prompt": 0.001, "completion": 0.002}',
        enabled=True,
    )

    from routstr.core.db import create_session

    async with create_session() as session:
        session.add(model)
        await session.commit()
        await session.refresh(model)

    update_data = {
        "id": "test-model-update",
        "name": "Updated Name",
        "description": "Updated description",
        "created": 1234567890,
        "context_length": 8192,
        "architecture": {
            "modality": "text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
        "pricing": {
            "prompt": 0.002,
            "completion": 0.003,
            "request": 0.0,
            "image": 0.0,
            "web_search": 0.0,
            "internal_reasoning": 0.0,
        },
        "enabled": True,
    }

    response = await integration_client.patch(
        f"/admin/api/upstream-providers/{test_provider}/models/test-model-update",
        json=update_data,
        headers=admin_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Name"
    assert data["context_length"] == 8192


@pytest.mark.asyncio
async def test_delete_provider_model(
    integration_client: AsyncClient,
    admin_headers: dict[str, str],
    test_provider: int,
) -> None:
    """Test deleting a provider model"""
    from routstr.core.db import ModelRow, create_session

    model = ModelRow(
        id="test-model-delete",
        upstream_provider_id=test_provider,
        name="To Delete",
        description="Will be deleted",
        created=1234567890,
        context_length=4096,
        architecture='{"modality": "text"}',
        pricing='{"prompt": 0.001, "completion": 0.002}',
        enabled=True,
    )

    async with create_session() as session:
        session.add(model)
        await session.commit()

    response = await integration_client.delete(
        f"/admin/api/upstream-providers/{test_provider}/models/test-model-delete",
        headers=admin_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["deleted_id"] == "test-model-delete"


@pytest.mark.asyncio
async def test_enable_disable_model(
    integration_client: AsyncClient,
    admin_headers: dict[str, str],
    test_provider: int,
) -> None:
    """Test enabling and disabling a model"""
    from routstr.core.db import ModelRow, create_session

    model = ModelRow(
        id="test-model-toggle",
        upstream_provider_id=test_provider,
        name="Toggle Model",
        description="For toggling",
        created=1234567890,
        context_length=4096,
        architecture='{"modality": "text"}',
        pricing='{"prompt": 0.001, "completion": 0.002}',
        enabled=True,
    )

    async with create_session() as session:
        session.add(model)
        await session.commit()
        await session.refresh(model)

    update_data = {
        "id": "test-model-toggle",
        "name": "Toggle Model",
        "description": "For toggling",
        "created": 1234567890,
        "context_length": 4096,
        "architecture": {"modality": "text"},
        "pricing": {
            "prompt": 0.001,
            "completion": 0.002,
            "request": 0.0,
            "image": 0.0,
            "web_search": 0.0,
            "internal_reasoning": 0.0,
        },
        "enabled": False,
    }

    response = await integration_client.patch(
        f"/admin/api/upstream-providers/{test_provider}/models/test-model-toggle",
        json=update_data,
        headers=admin_headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
