"""Tests for the admin settings endpoint's handling of secrets (issue #553).

``admin_password`` is no longer a settings field (it lives only as a one-way
hash in the Secret store), so it must never appear in the GET/PATCH payloads.
``nsec`` and ``upstream_api_key`` remain live in-memory runtime values but are
redacted on read and ignored on write — they cannot be set through the general
settings endpoint, only through their dedicated rotation paths.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient

from routstr.core.admin import admin_sessions
from routstr.core.db import AsyncSession
from routstr.core.settings import SettingsService, settings


@pytest_asyncio.fixture
async def admin_client(
    integration_client: AsyncClient,
) -> AsyncGenerator[AsyncClient, None]:
    """An integration_client pre-authenticated with an admin session token."""
    token = secrets.token_urlsafe(24)
    admin_sessions[token] = int(time.time()) + 3600
    integration_client.headers["Authorization"] = f"Bearer {token}"
    yield integration_client
    admin_sessions.pop(token, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_settings_omits_admin_password_and_redacts_secrets(
    admin_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "nsec", "nsec-secret")
    monkeypatch.setattr(settings, "upstream_api_key", "sk-secret")

    resp = await admin_client.get("/admin/api/settings")
    assert resp.status_code == 200

    data = resp.json()
    assert "admin_password" not in data
    assert data["nsec"] == "[REDACTED]"
    assert data["upstream_api_key"] == "[REDACTED]"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_settings_ignores_secret_fields(
    admin_client: AsyncClient,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The PATCH path persists through SettingsService, which needs an
    # initialized current snapshot and a settings row in the shared test DB.
    await SettingsService.initialize(integration_session)
    monkeypatch.setattr(settings, "nsec", "original-nsec")

    resp = await admin_client.patch(
        "/admin/api/settings",
        json={
            "name": "Renamed",
            "nsec": "attacker-nsec",
            "upstream_api_key": "attacker-key",
            "admin_password": "attacker-pw",
        },
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data["name"] == "Renamed"
    assert "admin_password" not in data
    assert data["nsec"] == "[REDACTED]"
    # The live secret was not overwritten through the general settings endpoint.
    assert settings.nsec == "original-nsec"
