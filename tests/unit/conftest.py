from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel

from routstr.core.settings import settings
from routstr.upstream import usage_multiplier_sources


@pytest.fixture(autouse=True)
def _disable_usage_multiplier_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep catalog refreshes off OpenRouter; multiplier tests opt back in."""
    monkeypatch.setattr(settings, "usage_multiplier_source", "none")
    monkeypatch.setattr(usage_multiplier_sources, "_instances", {})


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def analytics_engine(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[AsyncEngine]:
    if request.param == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analytics.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            yield engine
        finally:
            await engine.dispose()
        return

    url = os.environ.get("ROUTSTR_TEST_POSTGRES_URL")
    if not url:
        pytest.skip(
            "Set ROUTSTR_TEST_POSTGRES_URL for isolated PostgreSQL analytics tests"
        )
    schema = f"analytics_test_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin.dispose()


@pytest.fixture(autouse=True)
def _untracked_x_cashu_settlement(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Unit tests mock the ledger writes, so their payments are untracked.

    Tests of the settlement state itself restore the real functions against a
    real database (see test_x_cashu_settlement.py).
    """
    from routstr.payment import x_cashu_settlement

    async def no_transition(*_: object) -> bool:
        return False

    async def untracked(*_: object) -> None:
        return None

    monkeypatch.setattr(x_cashu_settlement, "_transition", no_transition)
    monkeypatch.setattr(x_cashu_settlement, "_settlement_state", untracked)
    yield
