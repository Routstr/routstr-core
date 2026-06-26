"""Tests for the admin nsec rotation endpoint (issue #553).

The Nostr identity is a secret: it lives encrypted in the Secret store, never in
the settings blob, so it cannot be set through the general settings PATCH. This
dedicated endpoint is the supported way to set/rotate/clear it — it encrypts the
key at rest, updates the live runtime identity (so signing picks it up without a
restart), and derives the npub. Invalid keys are rejected.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient

from routstr.core import vault
from routstr.core.admin import admin_sessions
from routstr.core.db import AsyncSession, get_secret
from routstr.core.settings import derive_npub_from_nsec, settings

# A valid 64-char hex private key (accepted by nsec_to_keypair, as in bootstrap).
NSEC_HEX = "1" * 64


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
async def test_update_nsec_stores_encrypted_and_derives_npub(
    admin_client: AsyncClient,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "nsec", "")
    monkeypatch.setattr(settings, "npub", "")

    resp = await admin_client.patch("/admin/api/nsec", json={"nsec": NSEC_HEX})
    assert resp.status_code == 200

    expected_npub = derive_npub_from_nsec(NSEC_HEX)
    assert resp.json() == {"ok": True, "npub": expected_npub}

    # Stored encrypted at rest, decryptable back to the original key.
    integration_session.expunge_all()
    secret = await get_secret(integration_session)
    assert secret.encrypted_nsec is not None
    assert vault.is_encrypted(secret.encrypted_nsec)
    assert vault.decrypt(secret.encrypted_nsec) == NSEC_HEX

    # Live runtime identity updated so Nostr signing reflects it without restart.
    assert settings.nsec == NSEC_HEX
    assert settings.npub == expected_npub


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_nsec_rejects_invalid_key(
    admin_client: AsyncClient,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "nsec", "")
    monkeypatch.setattr(settings, "npub", "")

    resp = await admin_client.patch(
        "/admin/api/nsec", json={"nsec": "not-a-real-nsec"}
    )
    assert resp.status_code == 400

    # Nothing stored, live identity untouched.
    integration_session.expunge_all()
    secret = await get_secret(integration_session)
    assert secret.encrypted_nsec is None
    assert settings.nsec == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_nsec_clears_identity_with_empty_value(
    admin_client: AsyncClient,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Start from a node that has an identity...
    monkeypatch.setattr(settings, "nsec", "")
    monkeypatch.setattr(settings, "npub", "")
    set_resp = await admin_client.patch("/admin/api/nsec", json={"nsec": NSEC_HEX})
    assert set_resp.status_code == 200

    # ...then clear it.
    clear_resp = await admin_client.patch("/admin/api/nsec", json={"nsec": ""})
    assert clear_resp.status_code == 200
    assert clear_resp.json() == {"ok": True, "npub": ""}

    integration_session.expunge_all()
    secret = await get_secret(integration_session)
    assert secret.encrypted_nsec is None
    assert settings.nsec == ""
    assert settings.npub == ""
