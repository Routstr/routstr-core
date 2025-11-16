"""Integration tests for admin authentication and authorization."""

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import ADMIN_SESSION_DURATION, admin_sessions
from routstr.core.settings import SettingsService


@pytest.mark.asyncio
async def test_admin_setup_first_time(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test initial admin setup with password."""
    await SettingsService.update({"admin_password": ""}, integration_session)
    
    response = await integration_client.post(
        "/admin/api/setup",
        json={"password": "test_password_123"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_admin_setup_rejects_short_password(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test that setup rejects passwords shorter than 8 characters."""
    await SettingsService.update({"admin_password": ""}, integration_session)
    
    response = await integration_client.post(
        "/admin/api/setup",
        json={"password": "short"},
    )
    
    assert response.status_code == 400
    assert "must be at least 8 characters" in response.json()["detail"]


@pytest.mark.asyncio
async def test_admin_setup_rejects_when_already_configured(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test that setup fails when admin password is already set."""
    await SettingsService.update({"admin_password": "existing_password"}, integration_session)
    
    response = await integration_client.post(
        "/admin/api/setup",
        json={"password": "new_password_123"},
    )
    
    assert response.status_code == 409
    assert "already set" in response.json()["detail"]


@pytest.mark.asyncio
async def test_admin_login_with_valid_password(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test admin login with valid password returns token."""
    test_password = "test_admin_password_123"
    await SettingsService.update({"admin_password": test_password}, integration_session)
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "token" in data
    assert data["expires_in"] == ADMIN_SESSION_DURATION
    assert len(data["token"]) > 20


@pytest.mark.asyncio
async def test_admin_login_with_invalid_password(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test admin login with invalid password fails."""
    await SettingsService.update({"admin_password": "correct_password"}, integration_session)
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": "wrong_password"},
    )
    
    assert response.status_code == 401
    assert "Invalid password" in response.json()["detail"]


@pytest.mark.asyncio
async def test_admin_login_when_not_configured(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test admin login fails when password not configured."""
    await SettingsService.update({"admin_password": ""}, integration_session)
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": "any_password"},
    )
    
    assert response.status_code == 500
    assert "not configured" in response.json()["detail"]


@pytest.mark.asyncio
async def test_admin_logout(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test admin logout removes session token."""
    test_password = "test_password_123"
    await SettingsService.update({"admin_password": test_password}, integration_session)
    
    login_response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    token = login_response.json()["token"]
    
    logout_response = await integration_client.post(
        "/admin/api/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert logout_response.status_code == 200
    assert logout_response.json()["ok"] is True
    
    settings_response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert settings_response.status_code == 403


@pytest.mark.asyncio
async def test_admin_endpoints_require_authentication(
    integration_client: AsyncClient,
) -> None:
    """Test that admin endpoints reject unauthenticated requests."""
    endpoints = [
        "/admin/api/settings",
        "/admin/api/balances",
        "/admin/api/upstream-providers",
        "/admin/partials/balances",
    ]
    
    for endpoint in endpoints:
        response = await integration_client.get(endpoint)
        assert response.status_code == 403, f"Endpoint {endpoint} should require auth"


@pytest.mark.asyncio
async def test_admin_endpoints_reject_invalid_token(
    integration_client: AsyncClient,
) -> None:
    """Test that admin endpoints reject invalid tokens."""
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": "Bearer invalid_token_123"},
    )
    
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_session_expiry(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test that expired admin sessions are rejected."""
    test_password = "test_password_123"
    await SettingsService.update({"admin_password": test_password}, integration_session)
    
    login_response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    token = login_response.json()["token"]
    
    admin_sessions[token] = 0
    
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_password_update_with_correct_current(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test password update with correct current password."""
    current_password = "current_password_123"
    new_password = "new_password_456"
    await SettingsService.update({"admin_password": current_password}, integration_session)
    
    login_response = await integration_client.post(
        "/admin/api/login",
        json={"password": current_password},
    )
    token = login_response.json()["token"]
    
    update_response = await integration_client.patch(
        "/admin/api/password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": current_password,
            "new_password": new_password,
        },
    )
    
    assert update_response.status_code == 200
    assert update_response.json()["ok"] is True
    
    login_response = await integration_client.post(
        "/admin/api/login",
        json={"password": new_password},
    )
    assert login_response.status_code == 200


@pytest.mark.asyncio
async def test_admin_password_update_with_wrong_current(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test password update with incorrect current password."""
    current_password = "current_password_123"
    await SettingsService.update({"admin_password": current_password}, integration_session)
    
    login_response = await integration_client.post(
        "/admin/api/login",
        json={"password": current_password},
    )
    token = login_response.json()["token"]
    
    update_response = await integration_client.patch(
        "/admin/api/password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": "wrong_password",
            "new_password": "new_password_456",
        },
    )
    
    assert update_response.status_code == 401
    assert "incorrect" in update_response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_admin_password_update_rejects_short_password(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test password update rejects passwords shorter than 6 characters."""
    current_password = "current_password_123"
    await SettingsService.update({"admin_password": current_password}, integration_session)
    
    login_response = await integration_client.post(
        "/admin/api/login",
        json={"password": current_password},
    )
    token = login_response.json()["token"]
    
    update_response = await integration_client.patch(
        "/admin/api/password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": current_password,
            "new_password": "short",
        },
    )
    
    assert update_response.status_code == 400
    assert "at least 6 characters" in update_response.json()["detail"]


@pytest.mark.asyncio
async def test_admin_password_update_requires_authentication(
    integration_client: AsyncClient,
) -> None:
    """Test password update requires authentication."""
    response = await integration_client.patch(
        "/admin/api/password",
        json={
            "current_password": "current",
            "new_password": "new_password",
        },
    )
    
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_token_cleanup_on_login(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Test that expired tokens are cleaned up on new login."""
    test_password = "test_password_123"
    await SettingsService.update({"admin_password": test_password}, integration_session)
    
    admin_sessions["expired_token_1"] = 0
    admin_sessions["expired_token_2"] = 0
    
    login_response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    
    assert login_response.status_code == 200
    assert "expired_token_1" not in admin_sessions
    assert "expired_token_2" not in admin_sessions
