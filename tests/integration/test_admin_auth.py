"""Integration tests for admin authentication endpoints"""

import os
import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

os.environ["ADMIN_PASSWORD"] = "test-admin-password-123"


@pytest.mark.asyncio
async def test_admin_login_with_valid_password(integration_client: AsyncClient) -> None:
    """Test admin login with valid password"""
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": "test-admin-password-123"},
    )

    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["token"] is not None
    assert len(data["token"]) > 0


@pytest.mark.asyncio
async def test_admin_login_with_invalid_password(integration_client: AsyncClient) -> None:
    """Test admin login with invalid password"""
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": "wrong-password"},
    )

    assert response.status_code == 401
    assert "token" not in response.json()


@pytest.mark.asyncio
async def test_admin_login_with_empty_password(integration_client: AsyncClient) -> None:
    """Test admin login with empty password"""
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": ""},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_endpoints_require_authentication(integration_client: AsyncClient) -> None:
    """Test that admin endpoints require authentication"""
    endpoints = [
        "/admin/api/settings",
        "/admin/api/balances",
        "/admin/api/temporary-balances",
        "/admin/api/upstream-providers",
    ]

    for endpoint in endpoints:
        response = await integration_client.get(endpoint)
        assert response.status_code == 403, f"Endpoint {endpoint} should require authentication"


@pytest.mark.asyncio
async def test_admin_logout(integration_client: AsyncClient) -> None:
    """Test admin logout"""
    login_response = await integration_client.post(
        "/admin/api/login",
        json={"password": "test-admin-password-123"},
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]

    headers = {"Authorization": f"Bearer {token}"}
    logout_response = await integration_client.post("/admin/api/logout", headers=headers)
    assert logout_response.status_code == 200

    settings_response = await integration_client.get(
        "/admin/api/settings", headers=headers
    )
    assert settings_response.status_code == 403


@pytest.mark.asyncio
async def test_admin_session_expiry(integration_client: AsyncClient) -> None:
    """Test that admin sessions expire after configured duration"""
    from routstr.core.admin import ADMIN_SESSION_DURATION
    import time

    login_response = await integration_client.post(
        "/admin/api/login",
        json={"password": "test-admin-password-123"},
    )
    assert login_response.status_code == 200
    token = login_response.json()["token"]

    headers = {"Authorization": f"Bearer {token}"}

    response = await integration_client.get("/admin/api/settings", headers=headers)
    assert response.status_code == 200

    from routstr.core.admin import admin_sessions
    if token in admin_sessions:
        admin_sessions[token] = int(time.time()) - ADMIN_SESSION_DURATION - 1

    expired_response = await integration_client.get("/admin/api/settings", headers=headers)
    assert expired_response.status_code == 403


@pytest.mark.asyncio
async def test_initial_setup(integration_client: AsyncClient) -> None:
    """Test initial admin setup when no password is set"""
    from routstr.core.settings import settings

    original_password = settings.admin_password

    try:
        settings.admin_password = None

        response = await integration_client.post(
            "/admin/api/setup",
            json={"password": "new-admin-password-123"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "token" in data
    finally:
        settings.admin_password = original_password


@pytest.mark.asyncio
async def test_initial_setup_already_configured(integration_client: AsyncClient) -> None:
    """Test that setup fails if admin password is already set"""
    response = await integration_client.post(
        "/admin/api/setup",
        json={"password": "new-admin-password-123"},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_initial_setup_short_password(integration_client: AsyncClient) -> None:
    """Test that setup requires password of at least 8 characters"""
    from routstr.core.settings import settings

    original_password = settings.admin_password

    try:
        settings.admin_password = None

        response = await integration_client.post(
            "/admin/api/setup",
            json={"password": "short"},
        )

        assert response.status_code == 400
    finally:
        settings.admin_password = original_password
