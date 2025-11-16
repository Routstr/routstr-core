"""Admin model management tests."""

import pytest
from httpx import AsyncClient

from routstr.core.db import ModelRow, UpstreamProviderRow, create_session


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
async def test_list_models_admin(
    integration_client: AsyncClient,
) -> None:
    """Test listing models in admin."""
    token = get_admin_token(integration_client)
    
    response = await integration_client.get(
        "/admin/api/models",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_model_override(
    integration_client: AsyncClient,
) -> None:
    """Test creating a model override."""
    token = get_admin_token(integration_client)
    
    async with create_session() as session:
        provider = UpstreamProviderRow(
            name="Test Provider",
            base_url="https://api.test.com/v1",
            api_key="test-key",
            provider_type="openai",
            provider_fee=1.0,
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)
        provider_id = provider.id
    
    model_data = {
        "id": "test-model-override",
        "upstream_provider_id": provider_id,
        "name": "Test Model Override",
        "description": "Test description",
        "context_length": 4096,
        "architecture": {"modality": "text"},
        "pricing": {
            "prompt": 0.001,
            "completion": 0.002,
            "request": 0.0,
            "image": 0.0,
            "web_search": 0.0,
            "internal_reasoning": 0.0,
            "max_cost": 100.0,
        },
        "enabled": True,
    }
    
    response = await integration_client.post(
        "/admin/api/models",
        headers={"Authorization": f"Bearer {token}"},
        json=model_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == model_data["id"]
    assert data["name"] == model_data["name"]
    
    async with create_session() as session:
        model = await session.get(
            ModelRow, (model_data["id"], model_data["upstream_provider_id"])
        )
        assert model is not None
        assert model.name == model_data["name"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_model_pricing(
    integration_client: AsyncClient,
) -> None:
    """Test updating model pricing."""
    token = get_admin_token(integration_client)
    
    async with create_session() as session:
        provider = UpstreamProviderRow(
            name="Test Provider",
            base_url="https://api.test.com/v1",
            api_key="test-key",
            provider_type="openai",
            provider_fee=1.0,
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)
        provider_id = provider.id
        
        model = ModelRow(
            id="test-model-update",
            upstream_provider_id=provider_id,
            name="Test Model",
            created=1234567890,
            description="Test",
            context_length=4096,
            architecture='{"modality": "text"}',
            pricing='{"prompt": 0.001, "completion": 0.002, "request": 0.0, "image": 0.0, "web_search": 0.0, "internal_reasoning": 0.0, "max_cost": 100.0}',
            enabled=True,
        )
        session.add(model)
        await session.commit()
    
    update_data = {
        "pricing": {
            "prompt": 0.002,
            "completion": 0.003,
            "request": 0.0,
            "image": 0.0,
            "web_search": 0.0,
            "internal_reasoning": 0.0,
            "max_cost": 150.0,
        },
    }
    
    response = await integration_client.put(
        f"/admin/api/models/test-model-update/{provider_id}",
        headers={"Authorization": f"Bearer {token}"},
        json=update_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert float(data["pricing"]["prompt"]) == 0.002


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_model_override(
    integration_client: AsyncClient,
) -> None:
    """Test deleting a model override."""
    token = get_admin_token(integration_client)
    
    async with create_session() as session:
        provider = UpstreamProviderRow(
            name="Test Provider",
            base_url="https://api.test.com/v1",
            api_key="test-key",
            provider_type="openai",
            provider_fee=1.0,
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)
        provider_id = provider.id
        
        model = ModelRow(
            id="test-model-delete",
            upstream_provider_id=provider_id,
            name="Test Model To Delete",
            created=1234567890,
            description="Test",
            context_length=4096,
            architecture='{"modality": "text"}',
            pricing='{"prompt": 0.001, "completion": 0.002, "request": 0.0, "image": 0.0, "web_search": 0.0, "internal_reasoning": 0.0, "max_cost": 100.0}',
            enabled=True,
        )
        session.add(model)
        await session.commit()
    
    response = await integration_client.delete(
        f"/admin/api/models/test-model-delete/{provider_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code == 200
    
    async with create_session() as session:
        model = await session.get(
            ModelRow, ("test-model-delete", provider_id)
        )
        assert model is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enable_disable_model(
    integration_client: AsyncClient,
) -> None:
    """Test enabling/disabling a model."""
    token = get_admin_token(integration_client)
    
    async with create_session() as session:
        provider = UpstreamProviderRow(
            name="Test Provider",
            base_url="https://api.test.com/v1",
            api_key="test-key",
            provider_type="openai",
            provider_fee=1.0,
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)
        provider_id = provider.id
        
        model = ModelRow(
            id="test-model-toggle",
            upstream_provider_id=provider_id,
            name="Test Model",
            created=1234567890,
            description="Test",
            context_length=4096,
            architecture='{"modality": "text"}',
            pricing='{"prompt": 0.001, "completion": 0.002, "request": 0.0, "image": 0.0, "web_search": 0.0, "internal_reasoning": 0.0, "max_cost": 100.0}',
            enabled=True,
        )
        session.add(model)
        await session.commit()
    
    response = await integration_client.patch(
        f"/admin/api/models/test-model-toggle/{provider_id}/enable",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": False},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    
    async with create_session() as session:
        model = await session.get(
            ModelRow, ("test-model-toggle", provider_id)
        )
        assert model is not None
        assert model.enabled is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_override_validation(
    integration_client: AsyncClient,
) -> None:
    """Test model override validation."""
    token = get_admin_token(integration_client)
    
    invalid_data = {
        "id": "",
        "name": "",
    }
    
    response = await integration_client.post(
        "/admin/api/models",
        headers={"Authorization": f"Bearer {token}"},
        json=invalid_data,
    )
    
    assert response.status_code in [400, 422]
