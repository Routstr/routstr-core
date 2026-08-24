"""Admin CRUD must never put an upstream API key into the database in the clear.

Creating and updating a provider through ``/admin/api/upstream-providers`` used
to copy the submitted key straight into a plaintext column. These tests read the
row back with raw SQL — the same view an operator with the database file has —
and assert only ciphertext and a keyed fingerprint are there, while the
duplicate check and the redacted API responses keep working.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core import vault
from routstr.core.admin import admin_sessions

API_KEY = "sk-admin-crud-secret"


@pytest_asyncio.fixture
async def admin_client(
    integration_client: AsyncClient,
) -> AsyncGenerator[AsyncClient, None]:
    token = secrets.token_urlsafe(24)
    admin_sessions[token] = int(time.time()) + 3600
    integration_client.headers["Authorization"] = f"Bearer {token}"
    yield integration_client
    admin_sessions.pop(token, None)


async def _stored_columns(session: AsyncSession, provider_id: int) -> tuple[str, str]:
    result = await session.exec(  # type: ignore[call-overload]
        text(
            "SELECT encrypted_api_key, api_key_fingerprint FROM upstream_providers "
            "WHERE id = :id"
        ).bindparams(id=provider_id)
    )
    row = result.first()
    assert row is not None
    return str(row[0]), str(row[1])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_created_provider_key_is_ciphertext_in_the_database(
    admin_client: AsyncClient, integration_session: AsyncSession
) -> None:
    response = await admin_client.post(
        "/admin/api/upstream-providers",
        json={
            "provider_type": "custom",
            "base_url": "https://created.example",
            "api_key": API_KEY,
        },
    )
    assert response.status_code == 200
    assert response.json()["api_key"] == "[REDACTED]"

    stored, fingerprint = await _stored_columns(
        integration_session, response.json()["id"]
    )
    assert API_KEY not in stored
    assert vault.decrypt(stored) == API_KEY
    assert fingerprint == vault.fingerprint(API_KEY)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_updated_provider_key_is_ciphertext_in_the_database(
    admin_client: AsyncClient, integration_session: AsyncSession
) -> None:
    created = await admin_client.post(
        "/admin/api/upstream-providers",
        json={
            "provider_type": "custom",
            "base_url": "https://updated.example",
            "api_key": "sk-initial",
        },
    )
    provider_id = created.json()["id"]

    response = await admin_client.patch(
        f"/admin/api/upstream-providers/{provider_id}",
        json={"api_key": API_KEY},
    )
    assert response.status_code == 200
    assert response.json()["api_key"] == "[REDACTED]"

    stored, fingerprint = await _stored_columns(integration_session, provider_id)
    assert API_KEY not in stored
    assert vault.decrypt(stored) == API_KEY
    assert fingerprint == vault.fingerprint(API_KEY)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_provider_is_still_detected_through_the_fingerprint(
    admin_client: AsyncClient,
) -> None:
    payload = {
        "provider_type": "custom",
        "base_url": "https://duplicate.example",
        "api_key": API_KEY,
    }
    first = await admin_client.post("/admin/api/upstream-providers", json=payload)
    assert first.status_code == 200

    duplicate = await admin_client.post("/admin/api/upstream-providers", json=payload)
    assert duplicate.status_code == 409

    other_key = await admin_client.post(
        "/admin/api/upstream-providers",
        json={**payload, "api_key": "sk-different"},
    )
    assert other_key.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_listing_providers_redacts_every_api_key(
    admin_client: AsyncClient,
) -> None:
    await admin_client.post(
        "/admin/api/upstream-providers",
        json={
            "provider_type": "custom",
            "base_url": "https://listed.example",
            "api_key": API_KEY,
        },
    )

    response = await admin_client.get("/admin/api/upstream-providers")
    assert response.status_code == 200

    body = response.text
    assert API_KEY not in body
    assert all(
        provider["api_key"] in ("[REDACTED]", "") for provider in response.json()
    )
