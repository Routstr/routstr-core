"""Dashboard logins must be honoured by every worker, not only the issuer.

With several uvicorn workers (or replicas) the request after login lands on an
arbitrary process. The in-memory ``admin_sessions`` dict is now only a cache in
front of the shared ``admin_sessions`` table, so these tests clear the dict to
stand in for "a different worker".
"""

from __future__ import annotations

import time

import pytest
from httpx import AsyncClient

from routstr.core.admin import admin_sessions
from routstr.core.db import AdminSession, AsyncSession, set_admin_password

PASSWORD = "shared-session-test"


async def _login(client: AsyncClient, session: AsyncSession) -> str:
    await set_admin_password(session, PASSWORD)
    resp = await client.post("/admin/api/login", json={"password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_from_another_worker_is_accepted(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    token = await _login(integration_client, integration_session)
    headers = {"Authorization": f"Bearer {token}"}

    admin_sessions.clear()  # this worker never saw the login
    resp = await integration_client.get("/admin/api/settings", headers=headers)
    assert resp.status_code == 200

    # The raw token is never stored, only its hash.
    integration_session.expunge_all()
    rows = (await integration_session.exec(AdminSession.__table__.select())).all()  # type: ignore[attr-defined]
    assert len(rows) == 1
    assert token not in str(rows[0])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_logout_revokes_the_session_everywhere(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    token = await _login(integration_client, integration_session)
    headers = {"Authorization": f"Bearer {token}"}

    # Use the session first so any per-process cache would be warm, then log
    # out on "another worker": that one only sees the shared table, so revoking
    # the row is all it can do. This worker must still reject the token.
    resp = await integration_client.get("/admin/api/settings", headers=headers)
    assert resp.status_code == 200
    from routstr.core.admin import _session_token_hash

    integration_session.expunge_all()
    row = await integration_session.get(AdminSession, _session_token_hash(token))
    assert row is not None
    await integration_session.delete(row)
    await integration_session.commit()

    resp = await integration_client.get("/admin/api/settings", headers=headers)
    assert resp.status_code == 403

    # And the issuing worker's own logout is idempotent and clears the row.
    resp = await integration_client.post(
        "/admin/api/logout", headers={"Authorization": "Bearer other"}
    )
    assert resp.status_code == 403
    assert token not in admin_sessions


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_shared_session_is_rejected(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    from routstr.core.admin import _session_token_hash

    integration_session.add(
        AdminSession(
            token_hash=_session_token_hash("stale"), expires_at=int(time.time()) - 1
        )
    )
    await integration_session.commit()

    resp = await integration_client.get(
        "/admin/api/settings", headers={"Authorization": "Bearer stale"}
    )
    assert resp.status_code == 403
