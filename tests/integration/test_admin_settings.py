"""Integration tests for admin settings management"""

import os
import pytest
from httpx import AsyncClient

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
async def test_get_admin_settings(
    integration_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Test getting admin settings"""
    response = await integration_client.get(
        "/admin/api/settings", headers=admin_headers
    )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert "admin_password" not in data or data["admin_password"] == "[REDACTED]"
    assert "upstream_api_key" not in data or data["upstream_api_key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_update_admin_settings(
    integration_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Test updating admin settings"""
    from routstr.core.settings import settings

    original_name = settings.name

    try:
        update_data = {"name": "Updated Test Name"}

        response = await integration_client.patch(
            "/admin/api/settings",
            json=update_data,
            headers=admin_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Test Name"
    finally:
        from routstr.core.settings import SettingsService
        from routstr.core.db import create_session

        async with create_session() as session:
            await SettingsService.update({"name": original_name}, session)


@pytest.mark.asyncio
async def test_update_password(
    integration_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Test updating admin password"""
    from routstr.core.settings import settings

    original_password = settings.admin_password

    try:
        update_data = {
            "current_password": "test-admin-password-123",
            "new_password": "new-test-password-456",
        }

        response = await integration_client.patch(
            "/admin/api/password",
            json=update_data,
            headers=admin_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True

        new_login_response = await integration_client.post(
            "/admin/api/login",
            json={"password": "new-test-password-456"},
        )
        assert new_login_response.status_code == 200
    finally:
        from routstr.core.settings import SettingsService
        from routstr.core.db import create_session

        async with create_session() as session:
            await SettingsService.update({"admin_password": original_password}, session)


@pytest.mark.asyncio
async def test_update_password_wrong_current(
    integration_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Test updating password with wrong current password"""
    update_data = {
        "current_password": "wrong-password",
        "new_password": "new-password-123",
    }

    response = await integration_client.patch(
        "/admin/api/password",
        json=update_data,
        headers=admin_headers,
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_update_password_short_new(
    integration_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Test updating password with too short new password"""
    update_data = {
        "current_password": "test-admin-password-123",
        "new_password": "short",
    }

    response = await integration_client.patch(
        "/admin/api/password",
        json=update_data,
        headers=admin_headers,
    )

    assert response.status_code == 400
