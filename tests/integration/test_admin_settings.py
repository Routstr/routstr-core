"""Admin settings management tests."""

import pytest
from httpx import AsyncClient


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
async def test_get_admin_settings(
    integration_client: AsyncClient,
) -> None:
    """Test getting admin settings."""
    token = get_admin_token(integration_client)
    
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "admin_password" not in data or data["admin_password"] == "[REDACTED]"
    assert "upstream_api_key" not in data or data["upstream_api_key"] == "[REDACTED]"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_admin_settings(
    integration_client: AsyncClient,
) -> None:
    """Test updating admin settings."""
    token = get_admin_token(integration_client)
    
    update_data = {
        "fixed_cost_per_request": 20,
        "tolerance_percentage": 5,
    }
    
    response = await integration_client.patch(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
        json=update_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["fixed_cost_per_request"] == 20
    assert data["tolerance_percentage"] == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_settings_validation(
    integration_client: AsyncClient,
) -> None:
    """Test settings validation."""
    token = get_admin_token(integration_client)
    
    invalid_data = {
        "tolerance_percentage": -10,
    }
    
    response = await integration_client.patch(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
        json=invalid_data,
    )
    
    assert response.status_code in [400, 422]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_settings_persistence(
    integration_client: AsyncClient,
) -> None:
    """Test that settings persist across requests."""
    token = get_admin_token(integration_client)
    
    update_data = {
        "fixed_cost_per_request": 25,
    }
    
    response = await integration_client.patch(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
        json=update_data,
    )
    
    assert response.status_code == 200
    
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["fixed_cost_per_request"] == 25


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_password(
    integration_client: AsyncClient,
) -> None:
    """Test updating admin password."""
    import os
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    token = response.json()["token"]
    
    password_update = {
        "current_password": test_password,
        "new_password": "new_password_456",
    }
    
    response = await integration_client.patch(
        "/admin/api/password",
        headers={"Authorization": f"Bearer {token}"},
        json=password_update,
    )
    
    assert response.status_code == 200
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": "new_password_456"},
    )
    assert response.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_password_wrong_current(
    integration_client: AsyncClient,
) -> None:
    """Test updating password with wrong current password."""
    import os
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    token = response.json()["token"]
    
    password_update = {
        "current_password": "wrong_password",
        "new_password": "new_password_456",
    }
    
    response = await integration_client.patch(
        "/admin/api/password",
        headers={"Authorization": f"Bearer {token}"},
        json=password_update,
    )
    
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_password_too_short(
    integration_client: AsyncClient,
) -> None:
    """Test updating password with too short new password."""
    import os
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    token = response.json()["token"]
    
    password_update = {
        "current_password": test_password,
        "new_password": "short",
    }
    
    response = await integration_client.patch(
        "/admin/api/password",
        headers={"Authorization": f"Bearer {token}"},
        json=password_update,
    )
    
    assert response.status_code == 400
