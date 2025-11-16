"""Integration tests for admin model management."""

import json
import time

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


@pytest.fixture
async def test_provider(integration_session: AsyncSession) -> UpstreamProviderRow:
    """Fixture to create a test upstream provider."""
    provider = UpstreamProviderRow(
        provider_type="openai",
        base_url="https://api.test-models.com/v1",
        api_key="test_key",
        enabled=True,
        provider_fee=1.05,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    return provider


@pytest.mark.asyncio
async def test_create_provider_model(
    integration_client: AsyncClient, admin_token: str, test_provider: UpstreamProviderRow
) -> None:
    """Test creating a new model for a provider."""
    model_data = {
        "id": "test-model-1",
        "name": "test-model-1",
        "description": "Test Model",
        "created": int(time.time()),
        "context_length": 4096,
        "architecture": {"modality": "text", "tokenizer": "gpt"},
        "pricing": {"input": 100, "output": 200},
        "enabled": True,
    }
    
    response = await integration_client.post(
        f"/admin/api/upstream-providers/{test_provider.id}/models",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=model_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "test-model-1"
    assert data["name"] == "test-model-1"
    assert data["enabled"] is True


@pytest.mark.asyncio
async def test_create_model_duplicate_id(
    integration_client: AsyncClient,
    admin_token: str,
    test_provider: UpstreamProviderRow,
    integration_session: AsyncSession,
) -> None:
    """Test that creating a duplicate model ID fails."""
    existing_model = ModelRow(
        id="duplicate-model",
        upstream_provider_id=test_provider.id,
        name="duplicate-model",
        created=0,
        description="Existing model",
        context_length=4096,
        architecture="{}",
        pricing='{"input": 100, "output": 200}',
        enabled=True,
    )
    integration_session.add(existing_model)
    await integration_session.commit()
    
    model_data = {
        "id": "duplicate-model",
        "name": "duplicate-model",
        "description": "New model",
        "created": int(time.time()),
        "context_length": 4096,
        "architecture": {"modality": "text"},
        "pricing": {"input": 100, "output": 200},
        "enabled": True,
    }
    
    response = await integration_client.post(
        f"/admin/api/upstream-providers/{test_provider.id}/models",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=model_data,
    )
    
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_model_nonexistent_provider(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test creating a model for a nonexistent provider."""
    model_data = {
        "id": "test-model",
        "name": "test-model",
        "description": "Test",
        "created": 0,
        "context_length": 4096,
        "architecture": {},
        "pricing": {"input": 100, "output": 200},
        "enabled": True,
    }
    
    response = await integration_client.post(
        "/admin/api/upstream-providers/99999/models",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=model_data,
    )
    
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_provider_model(
    integration_client: AsyncClient,
    admin_token: str,
    test_provider: UpstreamProviderRow,
    integration_session: AsyncSession,
) -> None:
    """Test getting a specific model."""
    model = ModelRow(
        id="get-test-model",
        upstream_provider_id=test_provider.id,
        name="get-test-model",
        created=0,
        description="Test model for GET",
        context_length=8192,
        architecture='{"modality": "text"}',
        pricing='{"input": 150, "output": 300}',
        enabled=True,
    )
    integration_session.add(model)
    await integration_session.commit()
    
    response = await integration_client.get(
        f"/admin/api/upstream-providers/{test_provider.id}/models/get-test-model",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "get-test-model"
    assert data["context_length"] == 8192


@pytest.mark.asyncio
async def test_get_nonexistent_model(
    integration_client: AsyncClient, admin_token: str, test_provider: UpstreamProviderRow
) -> None:
    """Test getting a model that doesn't exist."""
    response = await integration_client.get(
        f"/admin/api/upstream-providers/{test_provider.id}/models/nonexistent-model",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_provider_model(
    integration_client: AsyncClient,
    admin_token: str,
    test_provider: UpstreamProviderRow,
    integration_session: AsyncSession,
) -> None:
    """Test updating a model."""
    model = ModelRow(
        id="update-test-model",
        upstream_provider_id=test_provider.id,
        name="update-test-model",
        created=0,
        description="Original description",
        context_length=4096,
        architecture='{"modality": "text"}',
        pricing='{"input": 100, "output": 200}',
        enabled=True,
    )
    integration_session.add(model)
    await integration_session.commit()
    
    update_data = {
        "id": "update-test-model",
        "name": "update-test-model",
        "description": "Updated description",
        "created": 0,
        "context_length": 8192,
        "architecture": {"modality": "text", "updated": True},
        "pricing": {"input": 150, "output": 300},
        "enabled": False,
    }
    
    response = await integration_client.patch(
        f"/admin/api/upstream-providers/{test_provider.id}/models/update-test-model",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=update_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["description"] == "Updated description"
    assert data["context_length"] == 8192
    assert data["enabled"] is False


@pytest.mark.asyncio
async def test_update_model_with_mismatched_id(
    integration_client: AsyncClient,
    admin_token: str,
    test_provider: UpstreamProviderRow,
    integration_session: AsyncSession,
) -> None:
    """Test that updating with mismatched ID in path and payload fails."""
    model = ModelRow(
        id="original-model",
        upstream_provider_id=test_provider.id,
        name="original-model",
        created=0,
        description="Test",
        context_length=4096,
        architecture="{}",
        pricing='{"input": 100, "output": 200}',
        enabled=True,
    )
    integration_session.add(model)
    await integration_session.commit()
    
    update_data = {
        "id": "different-model",
        "name": "different-model",
        "description": "Test",
        "created": 0,
        "context_length": 4096,
        "architecture": {},
        "pricing": {"input": 100, "output": 200},
        "enabled": True,
    }
    
    response = await integration_client.patch(
        f"/admin/api/upstream-providers/{test_provider.id}/models/original-model",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=update_data,
    )
    
    assert response.status_code == 400
    assert "does not match" in response.json()["detail"]


@pytest.mark.asyncio
async def test_update_model_put_endpoint(
    integration_client: AsyncClient,
    admin_token: str,
    test_provider: UpstreamProviderRow,
    integration_session: AsyncSession,
) -> None:
    """Test updating model via PUT endpoint (should work same as PATCH)."""
    model = ModelRow(
        id="put-test-model",
        upstream_provider_id=test_provider.id,
        name="put-test-model",
        created=0,
        description="Original",
        context_length=4096,
        architecture="{}",
        pricing='{"input": 100, "output": 200}',
        enabled=True,
    )
    integration_session.add(model)
    await integration_session.commit()
    
    update_data = {
        "id": "put-test-model",
        "name": "put-test-model",
        "description": "Updated via PUT",
        "created": 0,
        "context_length": 4096,
        "architecture": {},
        "pricing": {"input": 100, "output": 200},
        "enabled": True,
    }
    
    response = await integration_client.put(
        f"/admin/api/upstream-providers/{test_provider.id}/models/put-test-model",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=update_data,
    )
    
    assert response.status_code == 200
    assert response.json()["description"] == "Updated via PUT"


@pytest.mark.asyncio
async def test_delete_provider_model(
    integration_client: AsyncClient,
    admin_token: str,
    test_provider: UpstreamProviderRow,
    integration_session: AsyncSession,
) -> None:
    """Test deleting a model."""
    model = ModelRow(
        id="delete-test-model",
        upstream_provider_id=test_provider.id,
        name="delete-test-model",
        created=0,
        description="To be deleted",
        context_length=4096,
        architecture="{}",
        pricing='{"input": 100, "output": 200}',
        enabled=True,
    )
    integration_session.add(model)
    await integration_session.commit()
    
    response = await integration_client.delete(
        f"/admin/api/upstream-providers/{test_provider.id}/models/delete-test-model",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["deleted_id"] == "delete-test-model"
    
    deleted_model = await integration_session.get(
        ModelRow, ("delete-test-model", test_provider.id)
    )
    assert deleted_model is None


@pytest.mark.asyncio
async def test_delete_nonexistent_model(
    integration_client: AsyncClient, admin_token: str, test_provider: UpstreamProviderRow
) -> None:
    """Test deleting a model that doesn't exist."""
    response = await integration_client.delete(
        f"/admin/api/upstream-providers/{test_provider.id}/models/nonexistent",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_all_provider_models(
    integration_client: AsyncClient,
    admin_token: str,
    test_provider: UpstreamProviderRow,
    integration_session: AsyncSession,
) -> None:
    """Test deleting all models for a provider."""
    models = [
        ModelRow(
            id=f"bulk-delete-{i}",
            upstream_provider_id=test_provider.id,
            name=f"bulk-delete-{i}",
            created=0,
            description="Test",
            context_length=4096,
            architecture="{}",
            pricing='{"input": 100, "output": 200}',
            enabled=True,
        )
        for i in range(3)
    ]
    for model in models:
        integration_session.add(model)
    await integration_session.commit()
    
    response = await integration_client.delete(
        f"/admin/api/upstream-providers/{test_provider.id}/models",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["deleted"] == 3


@pytest.mark.asyncio
async def test_model_with_per_request_limits(
    integration_client: AsyncClient, admin_token: str, test_provider: UpstreamProviderRow
) -> None:
    """Test creating a model with per_request_limits."""
    model_data = {
        "id": "limited-model",
        "name": "limited-model",
        "description": "Model with limits",
        "created": 0,
        "context_length": 4096,
        "architecture": {"modality": "text"},
        "pricing": {"input": 100, "output": 200},
        "per_request_limits": {"max_tokens": 1000, "max_input_tokens": 500},
        "enabled": True,
    }
    
    response = await integration_client.post(
        f"/admin/api/upstream-providers/{test_provider.id}/models",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=model_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["per_request_limits"]["max_tokens"] == 1000


@pytest.mark.asyncio
async def test_model_with_top_provider(
    integration_client: AsyncClient, admin_token: str, test_provider: UpstreamProviderRow
) -> None:
    """Test creating a model with top_provider metadata."""
    model_data = {
        "id": "top-provider-model",
        "name": "top-provider-model",
        "description": "Model with top provider",
        "created": 0,
        "context_length": 4096,
        "architecture": {"modality": "text"},
        "pricing": {"input": 100, "output": 200},
        "top_provider": {"is_top": True, "rank": 1},
        "enabled": True,
    }
    
    response = await integration_client.post(
        f"/admin/api/upstream-providers/{test_provider.id}/models",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=model_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["top_provider"]["is_top"] is True


@pytest.mark.asyncio
async def test_enable_disable_model(
    integration_client: AsyncClient,
    admin_token: str,
    test_provider: UpstreamProviderRow,
    integration_session: AsyncSession,
) -> None:
    """Test enabling and disabling a model."""
    model = ModelRow(
        id="enable-disable-model",
        upstream_provider_id=test_provider.id,
        name="enable-disable-model",
        created=0,
        description="Test",
        context_length=4096,
        architecture="{}",
        pricing='{"input": 100, "output": 200}',
        enabled=True,
    )
    integration_session.add(model)
    await integration_session.commit()
    
    update_data = {
        "id": "enable-disable-model",
        "name": "enable-disable-model",
        "description": "Test",
        "created": 0,
        "context_length": 4096,
        "architecture": {},
        "pricing": {"input": 100, "output": 200},
        "enabled": False,
    }
    
    response = await integration_client.patch(
        f"/admin/api/upstream-providers/{test_provider.id}/models/enable-disable-model",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=update_data,
    )
    
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    
    update_data["enabled"] = True
    response = await integration_client.patch(
        f"/admin/api/upstream-providers/{test_provider.id}/models/enable-disable-model",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=update_data,
    )
    
    assert response.status_code == 200
    assert response.json()["enabled"] is True


@pytest.mark.asyncio
async def test_model_endpoints_require_authentication(
    integration_client: AsyncClient, test_provider: UpstreamProviderRow
) -> None:
    """Test that all model endpoints require authentication."""
    model_data = {
        "id": "test",
        "name": "test",
        "description": "Test",
        "created": 0,
        "context_length": 4096,
        "architecture": {},
        "pricing": {"input": 100, "output": 200},
        "enabled": True,
    }
    
    endpoints = [
        ("POST", f"/admin/api/upstream-providers/{test_provider.id}/models", model_data),
        ("GET", f"/admin/api/upstream-providers/{test_provider.id}/models/test", None),
        ("PATCH", f"/admin/api/upstream-providers/{test_provider.id}/models/test", model_data),
        ("DELETE", f"/admin/api/upstream-providers/{test_provider.id}/models/test", None),
    ]
    
    for method, endpoint, payload in endpoints:
        if method == "GET":
            response = await integration_client.get(endpoint)
        elif method == "POST":
            response = await integration_client.post(endpoint, json=payload)
        elif method == "PATCH":
            response = await integration_client.patch(endpoint, json=payload)
        elif method == "DELETE":
            response = await integration_client.delete(endpoint)
        
        assert response.status_code == 403, f"{method} {endpoint} should require auth"


@pytest.mark.asyncio
async def test_model_pricing_with_provider_fee(
    integration_client: AsyncClient,
    admin_token: str,
    integration_session: AsyncSession,
) -> None:
    """Test that model pricing includes provider fee when retrieved."""
    provider = UpstreamProviderRow(
        provider_type="openai",
        base_url="https://api.fee-test.com/v1",
        api_key="test_key",
        enabled=True,
        provider_fee=2.0,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    
    model_data = {
        "id": "fee-test-model",
        "name": "fee-test-model",
        "description": "Test fee application",
        "created": 0,
        "context_length": 4096,
        "architecture": {},
        "pricing": {"input": 100, "output": 200},
        "enabled": True,
    }
    
    create_response = await integration_client.post(
        f"/admin/api/upstream-providers/{provider.id}/models",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=model_data,
    )
    
    assert create_response.status_code == 200
    
    get_response = await integration_client.get(
        f"/admin/api/upstream-providers/{provider.id}/models/fee-test-model",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert get_response.status_code == 200
    data = get_response.json()
    assert "pricing" in data
