"""Tests for admin password auth backed by the hashed Secret store (issue #553).

Login and password change verify against the one-way ``Secret.admin_password_hash``
(scrypt) instead of a plaintext settings field, which also closes the old ``!=``
timing-attack comparison. The first hash is written by ``bootstrap_secrets`` at
startup (generated, or migrated from a legacy password); here it is seeded
directly into the store, then login checks against it and a password change
re-hashes so the old password stops working and the new one starts.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest
from httpx import AsyncClient, Response

from routstr.core import admin, vault
from routstr.core.db import create_session, set_admin_password
from routstr.core.login_limiter import LoginLimiter, login_limiter


@pytest.fixture(autouse=True)
def _reset_login_limiter() -> Iterator[None]:
    # The limiter is process-wide; failures from one test must not lock the
    # next test's client out.
    login_limiter.reset()
    yield
    login_limiter.reset()


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


# --- login throttling ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_locks_out_after_repeated_failures(
    integration_client: AsyncClient,
) -> None:
    await _seed_password("correct horse")
    for _ in range(login_limiter.free_attempts):
        assert (await _login(integration_client, "wrong horse")).status_code == 401

    # Locked out: even the right password is refused without being checked.
    resp = await _login(integration_client, "correct horse")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_works_again_after_lockout_expires(
    integration_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    limiter = LoginLimiter(clock=lambda: now[0])
    monkeypatch.setattr(admin, "login_limiter", limiter)
    await _seed_password("correct horse")
    for _ in range(limiter.free_attempts):
        await _login(integration_client, "wrong horse")
    resp = await _login(integration_client, "correct horse")
    assert resp.status_code == 429

    now[0] += int(resp.headers["Retry-After"])
    assert (await _login(integration_client, "correct horse")).status_code == 200
    # Success clears the address's failures.
    assert (await _login(integration_client, "wrong horse")).status_code == 401
    assert (await _login(integration_client, "correct horse")).status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_checks_password_off_the_event_loop(
    integration_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_password("correct horse")
    loop_thread = threading.get_ident()
    check_threads: list[int] = []
    real_verify = vault.verify_password

    def recording_verify(password: str, stored: str) -> bool:
        check_threads.append(threading.get_ident())
        return real_verify(password, stored)

    monkeypatch.setattr(vault, "verify_password", recording_verify)
    assert (await _login(integration_client, "correct horse")).status_code == 200
    assert check_threads and loop_thread not in check_threads


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_refuses_when_all_check_slots_are_busy(
    integration_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_password("correct horse")
    release = threading.Event()
    started = threading.Semaphore(0)

    def blocking_verify(password: str, stored: str) -> bool:
        started.release()
        release.wait(timeout=10)
        return False

    monkeypatch.setattr(vault, "verify_password", blocking_verify)
    busy = [
        asyncio.create_task(_login(integration_client, "wrong horse"))
        for _ in range(login_limiter.max_concurrent_checks)
    ]
    for _ in busy:
        await asyncio.to_thread(started.acquire, timeout=10)

    # The event loop still serves requests while every check slot is blocked,
    # and a further login is turned away instead of queueing more scrypt work.
    resp = await _login(integration_client, "wrong horse")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "1"

    release.set()
    assert [r.status_code for r in await asyncio.gather(*busy)] == [401] * len(busy)


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
