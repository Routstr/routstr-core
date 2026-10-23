"""Tests for admin password auth backed by the hashed Secret store (issue #553).

Login and password change verify against the one-way ``Secret.admin_password_hash``
(scrypt) instead of a plaintext settings field, which also closes the old ``!=``
timing-attack comparison. The first hash is written by ``bootstrap_secrets`` at
startup (generated, or migrated from a legacy password); here it is seeded
directly into the store, then login checks against it and a password change
re-hashes so the old password stops working and the new one starts.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient, Response

from routstr.core.admin import admin_sessions
from routstr.core.db import create_session, set_admin_password


async def _seed_password(password: str) -> None:
    # bootstrap_secrets owns first-password creation at startup; tests seed the
    # hash straight into the store the same way, then exercise login/change.
    async with create_session() as session:
        await set_admin_password(session, password)


async def _login(client: AsyncClient, password: str) -> Response:
    return await client.post("/admin/api/login", json={"password": password})


# --- login -----------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_500_when_no_password_configured(
    integration_client: AsyncClient,
) -> None:
    resp = await _login(integration_client, "anything")
    assert resp.status_code == 500


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_succeeds_with_correct_password(
    integration_client: AsyncClient,
) -> None:
    await _seed_password("correct horse")
    resp = await _login(integration_client, "correct horse")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["token"], str) and body["token"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_rejects_wrong_password(
    integration_client: AsyncClient,
) -> None:
    await _seed_password("correct horse")
    resp = await _login(integration_client, "wrong horse")
    assert resp.status_code == 401


# --- password change -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_password_rehashes_so_only_new_works(
    integration_client: AsyncClient,
) -> None:
    await _seed_password("old password")
    login = await _login(integration_client, "old password")
    token = login.json()["token"]
    integration_client.headers["Authorization"] = f"Bearer {token}"

    resp = await integration_client.patch(
        "/admin/api/password",
        json={"current_password": "old password", "new_password": "new password"},
    )
    assert resp.status_code == 200, resp.text

    # Drop admin auth so the login calls aren't treated as authenticated noise.
    integration_client.headers.pop("Authorization", None)
    assert (await _login(integration_client, "old password")).status_code == 401
    assert (await _login(integration_client, "new password")).status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_password_rejects_wrong_current(
    integration_client: AsyncClient,
) -> None:
    await _seed_password("old password")
    login = await _login(integration_client, "old password")
    token = login.json()["token"]
    integration_client.headers["Authorization"] = f"Bearer {token}"

    resp = await integration_client.patch(
        "/admin/api/password",
        json={"current_password": "not the password", "new_password": "new password"},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_password_rejects_short_new(
    integration_client: AsyncClient,
) -> None:
    await _seed_password("old password")
    login = await _login(integration_client, "old password")
    token = login.json()["token"]
    integration_client.headers["Authorization"] = f"Bearer {token}"

    resp = await integration_client.patch(
        "/admin/api/password",
        json={"current_password": "old password", "new_password": "x"},
    )
    assert resp.status_code == 400


async def _create_cli_token(client: AsyncClient, name: str) -> str:
    resp = await client.post("/admin/api/cli-tokens", json={"name": name})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


async def _authorized(client: AsyncClient, token: str) -> bool:
    resp = await client.get(
        "/admin/api/cli-tokens", headers={"Authorization": f"Bearer {token}"}
    )
    return resp.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_password_ends_other_sessions(
    integration_client: AsyncClient,
) -> None:
    await _seed_password("old password")
    caller = (await _login(integration_client, "old password")).json()["token"]
    other = (await _login(integration_client, "old password")).json()["token"]
    integration_client.headers["Authorization"] = f"Bearer {caller}"
    cli_token = await _create_cli_token(integration_client, "survives")

    resp = await integration_client.patch(
        "/admin/api/password",
        json={"current_password": "old password", "new_password": "new password"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked_cli_tokens"] == 0

    assert other not in admin_sessions
    assert not await _authorized(integration_client, other)
    # The session that changed the password stays signed in, and CLI tokens are
    # independent credentials unless revocation is asked for.
    assert await _authorized(integration_client, caller)
    assert await _authorized(integration_client, cli_token)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_password_can_revoke_all_cli_tokens(
    integration_client: AsyncClient,
) -> None:
    await _seed_password("old password")
    caller = (await _login(integration_client, "old password")).json()["token"]
    integration_client.headers["Authorization"] = f"Bearer {caller}"
    cli_tokens = [
        await _create_cli_token(integration_client, name) for name in ("a", "b")
    ]

    resp = await integration_client.patch(
        "/admin/api/password",
        json={
            "current_password": "old password",
            "new_password": "new password",
            "revoke_cli_tokens": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked_cli_tokens"] == 2
    for cli_token in cli_tokens:
        assert not await _authorized(integration_client, cli_token)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_password_change_keeps_sessions_and_cli_tokens(
    integration_client: AsyncClient,
) -> None:
    await _seed_password("old password")
    caller = (await _login(integration_client, "old password")).json()["token"]
    other = (await _login(integration_client, "old password")).json()["token"]
    integration_client.headers["Authorization"] = f"Bearer {caller}"
    cli_token = await _create_cli_token(integration_client, "kept")

    resp = await integration_client.patch(
        "/admin/api/password",
        json={
            "current_password": "not the password",
            "new_password": "new password",
            "revoke_cli_tokens": True,
        },
    )
    assert resp.status_code == 401
    assert await _authorized(integration_client, other)
    assert await _authorized(integration_client, cli_token)
