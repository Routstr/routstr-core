"""Admin authentication and authorization tests."""

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import create_session


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_login_with_valid_password(
    integration_client: AsyncClient,
) -> None:
    """Test admin login with valid password."""
    import os
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["ok"] is True
    
    token = data["token"]
    
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_login_with_invalid_password(
    integration_client: AsyncClient,
) -> None:
    """Test admin login with invalid password."""
    import os
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": "wrong_password"},
    )
    
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_session_expiry(
    integration_client: AsyncClient,
) -> None:
    """Test admin session expiry."""
    import os
    import time
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    
    assert response.status_code == 200
    token = response.json()["token"]
    
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    
    from routstr.core.admin import admin_sessions, ADMIN_SESSION_DURATION
    admin_sessions[token] = int(time.time()) - ADMIN_SESSION_DURATION - 1
    
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_logout(
    integration_client: AsyncClient,
) -> None:
    """Test admin logout."""
    import os
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = await integration_client.post(
        "/admin/api/login",
        json={"password": test_password},
    )
    
    assert response.status_code == 200
    token = response.json()["token"]
    
    response = await integration_client.post(
        "/admin/api/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    
    assert response.status_code == 200
    
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_endpoints_require_authentication(
    integration_client: AsyncClient,
) -> None:
    """Test that admin endpoints require authentication."""
    endpoints = [
        "/admin/api/settings",
        "/admin/api/balances",
        "/admin/api/temporary-balances",
        "/admin/partials/balances",
        "/admin/partials/apikeys",
    ]
    
    for endpoint in endpoints:
        response = await integration_client.get(endpoint)
        assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_admin_cannot_access_admin_endpoints(
    integration_client: AsyncClient,
) -> None:
    """Test that non-admin users cannot access admin endpoints."""
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": "Bearer invalid_token"},
    )
    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_setup_initial_password(
    integration_client: AsyncClient,
) -> None:
    """Test initial admin password setup."""
    import os
    
    original_password = os.environ.get("ADMIN_PASSWORD")
    os.environ.pop("ADMIN_PASSWORD", None)
    
    try:
        response = await integration_client.post(
            "/admin/api/setup",
            json={"password": "new_password_123"},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        
        response = await integration_client.post(
            "/admin/api/login",
            json={"password": "new_password_123"},
        )
        assert response.status_code == 200
    finally:
        if original_password:
            os.environ["ADMIN_PASSWORD"] = original_password


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_setup_already_configured(
    integration_client: AsyncClient,
) -> None:
    """Test that setup fails if password already configured."""
    import os
    
    test_password = "test_admin_password_123"
    os.environ["ADMIN_PASSWORD"] = test_password
    
    response = await integration_client.post(
        "/admin/api/setup",
        json={"password": "new_password_123"},
    )
    
    assert response.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_setup_password_too_short(
    integration_client: AsyncClient,
) -> None:
    """Test that setup requires password of at least 8 characters."""
    import os
    
    original_password = os.environ.get("ADMIN_PASSWORD")
    os.environ.pop("ADMIN_PASSWORD", None)
    
    try:
        response = await integration_client.post(
            "/admin/api/setup",
            json={"password": "short"},
        )
        
        assert response.status_code == 400
    finally:
        if original_password:
            os.environ["ADMIN_PASSWORD"] = original_password
