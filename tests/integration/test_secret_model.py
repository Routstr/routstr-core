"""Tests for the ``Secret`` singleton model (issue #553).

Specifies the node-level secret store: a single row (``id=1``, like
``RoutstrFee``) holding the one-way admin-password hash and the encrypted nsec.
``get_secret`` is get-or-create, so callers always get the singleton without
worrying whether it has been initialised yet. Encoding of the values themselves
lives in ``routstr.core.vault``; here we only assert the row persists and stays
a singleton.
"""

import time
from typing import Any

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import Secret, get_secret


@pytest.mark.asyncio
async def test_get_secret_creates_singleton(
    integration_session: AsyncSession,
) -> None:
    secret = await get_secret(integration_session)
    assert secret.id == 1
    # Fresh row carries no secret material yet.
    assert secret.admin_password_hash is None
    assert secret.encrypted_nsec is None
    assert secret.updated_at is None


@pytest.mark.asyncio
async def test_get_secret_is_idempotent(
    integration_session: AsyncSession,
) -> None:
    first = await get_secret(integration_session)
    second = await get_secret(integration_session)
    assert first.id == second.id == 1
    rows = (await integration_session.exec(select(Secret))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_secret_fields_round_trip(
    integration_session: AsyncSession,
) -> None:
    secret = await get_secret(integration_session)
    secret.admin_password_hash = "scrypt:16384:8:1:c2FsdA==:aGFzaA=="
    secret.encrypted_nsec = "fernet:v1:gAAAAA"
    secret.updated_at = int(time.time())
    integration_session.add(secret)
    await integration_session.commit()

    integration_session.expunge_all()
    reloaded = await get_secret(integration_session)
    assert reloaded.admin_password_hash == "scrypt:16384:8:1:c2FsdA==:aGFzaA=="
    assert reloaded.encrypted_nsec == "fernet:v1:gAAAAA"
    assert reloaded.updated_at is not None


@pytest.mark.asyncio
async def test_get_secret_tolerates_concurrent_first_insert(
    integration_engine: Any,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A second worker wins the race and commits the singleton row first.
    async with AsyncSession(integration_engine, expire_on_commit=False) as other:
        other.add(Secret(id=1, admin_password_hash="scrypt:from-other-worker"))
        await other.commit()

    # Reproduce the race window: our session's first read still sees no row, so
    # it attempts to INSERT a duplicate id=1. The real IntegrityError that follows
    # must be recovered (roll back, re-read) rather than crashing startup.
    real_get = integration_session.get
    calls = {"n": 0}

    async def stale_first_read(model: Any, pk: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_get(model, pk)

    monkeypatch.setattr(integration_session, "get", stale_first_read)

    secret = await get_secret(integration_session)

    # Recovered the other worker's row; no crash, still a single row.
    assert secret.id == 1
    assert secret.admin_password_hash == "scrypt:from-other-worker"
    rows = (await integration_session.exec(select(Secret))).all()
    assert len(rows) == 1
