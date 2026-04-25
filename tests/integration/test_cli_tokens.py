"""Integration tests for CLI token management (/admin/api/cli-tokens).

Covers:
- GET    /admin/api/cli-tokens           — list (preview only, no full token)
- POST   /admin/api/cli-tokens           — create (returns full token once)
- DELETE /admin/api/cli-tokens/{id}      — revoke
- Using a CLI token as Bearer auth against admin endpoints
- Expiry enforcement (expired tokens are rejected by require_admin_api)
- last_used_at bump on successful use
- Auth failures: missing token, wrong token, revoked token
"""

from __future__ import annotations

import secrets
import time
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlmodel import select

from routstr.core.admin import admin_sessions
from routstr.core.db import AsyncSession, CliToken


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def admin_session_token() -> AsyncGenerator[str, None]:
    """Inject a short-lived admin session token into admin_sessions."""
    token = secrets.token_urlsafe(24)
    admin_sessions[token] = int(time.time()) + 3600
    yield token
    admin_sessions.pop(token, None)


@pytest_asyncio.fixture
async def admin_client(
    integration_client: AsyncClient, admin_session_token: str
) -> AsyncClient:
    """An integration_client pre-authenticated with an admin session token."""
    integration_client.headers["Authorization"] = f"Bearer {admin_session_token}"
    return integration_client


# ──────────────────────────────────────────────────────────────────────────────
# Creation
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_cli_token_returns_full_token_once(
    admin_client: AsyncClient,
) -> None:
    """POST /admin/api/cli-tokens returns the raw token only on creation."""
    resp = await admin_client.post(
        "/admin/api/cli-tokens",
        json={"name": "my-laptop"},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["name"] == "my-laptop"
    assert isinstance(body["id"], str) and body["id"]
    assert isinstance(body["token"], str) and len(body["token"]) >= 32
    assert body["expires_at"] is None
    assert isinstance(body["created_at"], int)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_cli_token_with_expiry(admin_client: AsyncClient) -> None:
    """expires_in_days sets expires_at ~= now + days * 86400."""
    before = int(time.time())
    resp = await admin_client.post(
        "/admin/api/cli-tokens",
        json={"name": "ci-runner", "expires_in_days": 7},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["expires_at"] is not None
    delta = body["expires_at"] - before
    # Allow 10s jitter around 7 * 86400
    assert 7 * 86400 - 10 <= delta <= 7 * 86400 + 10


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_cli_token_rejects_empty_name(
    admin_client: AsyncClient,
) -> None:
    resp = await admin_client.post(
        "/admin/api/cli-tokens", json={"name": "   "}
    )
    assert resp.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_cli_token_requires_admin(
    integration_client: AsyncClient,
) -> None:
    """No admin token / no bearer → 403."""
    resp = await integration_client.post(
        "/admin/api/cli-tokens", json={"name": "no-auth"}
    )
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────────────────────
# Listing
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_cli_tokens_returns_preview_not_full_token(
    admin_client: AsyncClient,
) -> None:
    """Listing never leaks the raw token."""
    create = await admin_client.post(
        "/admin/api/cli-tokens", json={"name": "secret-keeper"}
    )
    assert create.status_code == 200
    full_token = create.json()["token"]

    resp = await admin_client.get("/admin/api/cli-tokens")
    assert resp.status_code == 200
    items = resp.json()
    assert any(t["name"] == "secret-keeper" for t in items)

    for t in items:
        # No 'token' field, only 'token_preview'
        assert "token" not in t
        assert "token_preview" in t
        assert full_token not in t["token_preview"]
        assert "..." in t["token_preview"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_cli_tokens_requires_admin(
    integration_client: AsyncClient,
) -> None:
    resp = await integration_client.get("/admin/api/cli-tokens")
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────────────────────
# Using a CLI token as admin auth
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_cli_token_authorizes_admin_endpoints(
    admin_client: AsyncClient,
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """A freshly-created CLI token can be used as Bearer on admin endpoints."""
    create = await admin_client.post(
        "/admin/api/cli-tokens", json={"name": "cli-auth"}
    )
    assert create.status_code == 200
    cli_token = create.json()["token"]
    token_id = create.json()["id"]

    # Use a NEW client to isolate the header from admin_session_token
    integration_client.headers["Authorization"] = f"Bearer {cli_token}"
    resp = await integration_client.get("/admin/api/cli-tokens")
    assert resp.status_code == 200

    # last_used_at should be populated after use
    row = await integration_session.get(CliToken, token_id)
    assert row is not None
    assert row.last_used_at is not None
    assert row.last_used_at >= row.created_at


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_cli_token_is_rejected(
    admin_client: AsyncClient,
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """A CLI token with expires_at in the past → 403."""
    create = await admin_client.post(
        "/admin/api/cli-tokens",
        json={"name": "will-expire", "expires_in_days": 1},
    )
    assert create.status_code == 200
    cli_token = create.json()["token"]
    token_id = create.json()["id"]

    # Force-expire it in the DB
    row = await integration_session.get(CliToken, token_id)
    assert row is not None
    row.expires_at = int(time.time()) - 1
    integration_session.add(row)
    await integration_session.commit()

    integration_client.headers["Authorization"] = f"Bearer {cli_token}"
    resp = await integration_client.get("/admin/api/cli-tokens")
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_bearer_token_is_rejected(
    integration_client: AsyncClient,
) -> None:
    integration_client.headers["Authorization"] = "Bearer not-a-real-token"
    resp = await integration_client.get("/admin/api/cli-tokens")
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────────────────────
# Revocation
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_revoke_cli_token_removes_auth(
    admin_client: AsyncClient,
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """After DELETE, the token no longer authorizes."""
    create = await admin_client.post(
        "/admin/api/cli-tokens", json={"name": "to-revoke"}
    )
    token_id = create.json()["id"]
    cli_token = create.json()["token"]

    revoke = await admin_client.delete(f"/admin/api/cli-tokens/{token_id}")
    assert revoke.status_code == 200
    assert revoke.json() == {"ok": True, "deleted_id": token_id}

    # Row is gone
    row = await integration_session.get(CliToken, token_id)
    assert row is None

    # Can no longer be used for auth
    integration_client.headers["Authorization"] = f"Bearer {cli_token}"
    resp = await integration_client.get("/admin/api/cli-tokens")
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revoke_unknown_cli_token_returns_404(
    admin_client: AsyncClient,
) -> None:
    resp = await admin_client.delete("/admin/api/cli-tokens/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revoke_cli_token_requires_admin(
    integration_client: AsyncClient,
) -> None:
    resp = await integration_client.delete("/admin/api/cli-tokens/anything")
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────────────────────
# Lifecycle / uniqueness
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_multiple_tokens_are_independent(
    admin_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """Creating N tokens yields N unique tokens that all live in DB."""
    names = ["dev-a", "dev-b", "dev-c"]
    raw_tokens: list[str] = []
    ids: list[str] = []
    for name in names:
        r = await admin_client.post(
            "/admin/api/cli-tokens", json={"name": name}
        )
        assert r.status_code == 200
        raw_tokens.append(r.json()["token"])
        ids.append(r.json()["id"])

    # All unique
    assert len(set(raw_tokens)) == len(raw_tokens)
    assert len(set(ids)) == len(ids)

    # All in DB
    result = await integration_session.exec(
        select(CliToken).where(CliToken.name.in_(names))  # type: ignore[attr-defined]
    )
    rows = result.all()
    assert {r.name for r in rows} == set(names)
