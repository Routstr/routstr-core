"""Tests for admin password auth backed by the hashed Secret store (issue #553).

Login, password change and first-run setup verify against the one-way
``Secret.admin_password_hash`` (scrypt) instead of a plaintext settings field,
which also closes the old ``!=`` timing-attack comparison. The flow is exercised
end-to-end through the public admin endpoints: setup writes the first hash,
login checks against it, and a password change re-hashes so the old password
stops working and the new one starts.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient, Response


async def _setup_password(client: AsyncClient, password: str) -> None:
    resp = await client.post("/admin/api/setup", json={"password": password})
    assert resp.status_code == 200, resp.text


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
    await _setup_password(integration_client, "correct horse")
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
    await _setup_password(integration_client, "correct horse")
    resp = await _login(integration_client, "wrong horse")
    assert resp.status_code == 401


# --- first-run setup -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_setup_rejects_short_password(
    integration_client: AsyncClient,
) -> None:
    resp = await integration_client.post("/admin/api/setup", json={"password": "short"})
    assert resp.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_setup_409_when_password_already_set(
    integration_client: AsyncClient,
) -> None:
    await _setup_password(integration_client, "first password")
    resp = await integration_client.post(
        "/admin/api/setup", json={"password": "second password"}
    )
    assert resp.status_code == 409


# --- password change -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_password_rehashes_so_only_new_works(
    integration_client: AsyncClient,
) -> None:
    await _setup_password(integration_client, "old password")
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
    await _setup_password(integration_client, "old password")
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
    await _setup_password(integration_client, "old password")
    login = await _login(integration_client, "old password")
    token = login.json()["token"]
    integration_client.headers["Authorization"] = f"Bearer {token}"

    resp = await integration_client.patch(
        "/admin/api/password",
        json={"current_password": "old password", "new_password": "x"},
    )
    assert resp.status_code == 400
