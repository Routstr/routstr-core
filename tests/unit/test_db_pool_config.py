from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.pool import StaticPool

from routstr.core import db
from routstr.core.db import create_db_engine
from routstr.core.settings import settings


@pytest.mark.asyncio
async def test_file_sqlite_serializes_connections_and_waits_for_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(settings, "database_pool_size", 12)
    monkeypatch.setattr(settings, "database_max_overflow", 3)
    monkeypatch.setattr(settings, "database_pool_timeout", 2.5)
    monkeypatch.setattr(settings, "database_pool_recycle", 900)
    monkeypatch.setattr(settings, "database_pool_pre_ping", False)
    monkeypatch.setattr(settings, "database_sqlite_busy_timeout", 30.0)

    engine = create_db_engine(f"sqlite+aiosqlite:///{tmp_path}/pool.db")
    try:
        assert engine.pool.size() == 1  # type: ignore[attr-defined]
        assert engine.pool._max_overflow == 0  # type: ignore[attr-defined]
        assert engine.pool._timeout == 2.5  # type: ignore[attr-defined]
        assert engine.pool._recycle == 900
        assert engine.pool._pre_ping is False
        async with engine.connect() as connection:
            busy_timeout = await connection.exec_driver_sql("PRAGMA busy_timeout")
            assert busy_timeout.scalar_one() == 30_000
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_sqlite_keeps_static_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_pool_pre_ping", True)
    engine = create_db_engine("sqlite+aiosqlite://")
    try:
        assert isinstance(engine.pool, StaticPool)
        assert engine.pool._pre_ping is True
    finally:
        await engine.dispose()


def test_non_sqlite_backend_enables_pre_ping_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_pool_pre_ping", False)
    monkeypatch.setattr(settings, "database_pool_size", 12)
    monkeypatch.setattr(settings, "database_max_overflow", 3)
    fake_engine = MagicMock()

    with (
        patch.object(db, "create_async_engine", return_value=fake_engine) as factory,
        patch.object(db.event, "listen") as listen,
    ):
        created = create_db_engine("postgresql+asyncpg://user:pass@db/node")

    assert created is fake_engine
    assert factory.call_args.kwargs["pool_pre_ping"] is True
    assert factory.call_args.kwargs["pool_size"] == 12
    assert factory.call_args.kwargs["max_overflow"] == 3
    assert listen.call_count == 2


@pytest.mark.asyncio
async def test_every_created_engine_warns_for_long_checkouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(settings, "database_pool_hold_warn_seconds", 0.0)
    monkeypatch.setattr(settings, "database_pool_pre_ping", False)
    first = create_db_engine(f"sqlite+aiosqlite:///{tmp_path}/first.db")
    second = create_db_engine(f"sqlite+aiosqlite:///{tmp_path}/second.db")

    try:
        with patch.object(db.logger, "warning") as warning:
            async with first.connect() as connection:
                await connection.exec_driver_sql("SELECT 1")
            async with second.connect() as connection:
                await connection.exec_driver_sql("SELECT 1")

        assert warning.call_count == 2
        assert all(
            call.kwargs["extra"]["threshold_seconds"] == 0.0
            for call in warning.call_args_list
        )
    finally:
        await first.dispose()
        await second.dispose()
