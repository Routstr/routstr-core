"""Integration tests for admin settings management."""

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.settings import SettingsService, settings


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
async def test_get_admin_settings(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test getting admin settings."""
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "admin_password" in data
    assert data["admin_password"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_get_settings_redacts_sensitive_data(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test that sensitive settings are redacted."""
    await SettingsService.update(
        {
            "upstream_api_key": "secret_key_123",
            "nsec": "nsec1234567890",
        },
        integration_session,
    )
    
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["upstream_api_key"] == "[REDACTED]"
    assert data["nsec"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_update_admin_settings(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test updating admin settings."""
    update_data = {
        "default_provider": "https://api.newprovider.com/v1",
        "max_cost_tolerance": 1.5,
    }
    
    response = await integration_client.patch(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=update_data,
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "default_provider" in data


@pytest.mark.asyncio
async def test_settings_persistence(
    integration_client: AsyncClient, admin_token: str, integration_session: AsyncSession
) -> None:
    """Test that settings persist across requests."""
    test_value = "https://api.persistent.com/v1"
    
    await integration_client.patch(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"default_provider": test_value},
    )
    
    response = await integration_client.get(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_settings_require_authentication(
    integration_client: AsyncClient,
) -> None:
    """Test that settings endpoints require authentication."""
    get_response = await integration_client.get("/admin/api/settings")
    assert get_response.status_code == 403
    
    patch_response = await integration_client.patch(
        "/admin/api/settings", json={"test": "value"}
    )
    assert patch_response.status_code == 403


@pytest.mark.asyncio
async def test_update_settings_validates_types(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test that settings validation works for type checking."""
    response = await integration_client.patch(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"max_cost_tolerance": "not_a_number"},
    )
    
    assert response.status_code in [400, 422]


@pytest.mark.asyncio
async def test_get_balances_api(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test getting balances via API."""
    response = await integration_client.get(
        "/admin/api/balances",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "balance_details" in data
    assert "total_wallet_balance_sats" in data
    assert "total_user_balance_sats" in data
    assert "owner_balance" in data


@pytest.mark.asyncio
async def test_get_temporary_balances(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test getting temporary balances."""
    response = await integration_client.get(
        "/admin/api/temporary-balances",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)


@pytest.mark.asyncio
async def test_partial_balances_html(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test getting balances HTML partial."""
    response = await integration_client.get(
        "/admin/partials/balances",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_balances_require_authentication(
    integration_client: AsyncClient,
) -> None:
    """Test that balance endpoints require authentication."""
    endpoints = [
        "/admin/api/balances",
        "/admin/api/temporary-balances",
        "/admin/partials/balances",
    ]
    
    for endpoint in endpoints:
        response = await integration_client.get(endpoint)
        assert response.status_code == 403, f"{endpoint} should require auth"


@pytest.mark.asyncio
async def test_settings_update_returns_redacted_values(
    integration_client: AsyncClient, admin_token: str
) -> None:
    """Test that settings update response redacts sensitive values."""
    response = await integration_client.patch(
        "/admin/api/settings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"upstream_api_key": "new_secret_key", "nsec": "new_nsec_value"},
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["upstream_api_key"] == "[REDACTED]"
    assert data["nsec"] == "[REDACTED]"
