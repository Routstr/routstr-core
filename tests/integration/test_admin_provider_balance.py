from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from routstr.core.admin import admin_sessions
from routstr.core.db import UpstreamProviderRow


async def _create_routstr_provider() -> UpstreamProviderRow:
    return UpstreamProviderRow(
        provider_type="routstr",
        base_url="https://upstream.example",
        api_key="",
        enabled=True,
    )


def _admin_headers() -> dict[str, str]:
    token = "test-admin-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_routstr_provider_balance_timeout_returns_504(
    integration_client: httpx.AsyncClient,
    integration_session,
) -> None:
    provider = await _create_routstr_provider()
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)

    request = httpx.Request("GET", f"{provider.base_url}/v1/balance/info")
    timeout_error = httpx.ConnectTimeout("Connect timeout", request=request)

    with patch(
        "httpx.AsyncHTTPTransport.handle_async_request",
        new=AsyncMock(side_effect=timeout_error),
    ):
        response = await integration_client.get(
            f"/admin/api/upstream-providers/{provider.id}/balance",
            headers=_admin_headers(),
        )

    assert response.status_code == 504
    assert response.json()["detail"] == "Timed out contacting upstream Routstr provider"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_routstr_provider_balance_request_error_returns_502(
    integration_client: httpx.AsyncClient,
    integration_session,
) -> None:
    provider = await _create_routstr_provider()
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)

    request = httpx.Request("GET", f"{provider.base_url}/v1/balance/info")
    request_error = httpx.ConnectError("Connection failed", request=request)

    with patch(
        "httpx.AsyncHTTPTransport.handle_async_request",
        new=AsyncMock(side_effect=request_error),
    ):
        response = await integration_client.get(
            f"/admin/api/upstream-providers/{provider.id}/balance",
            headers=_admin_headers(),
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "Failed to contact upstream Routstr provider"
