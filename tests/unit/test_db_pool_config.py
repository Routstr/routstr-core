from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.pool import StaticPool

from routstr.core import db
from routstr.core.db import create_db_engine
from routstr.core.settings import settings


@pytest.mark.asyncio
async def test_engine_uses_validated_bounded_pool_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(settings, "database_pool_size", 12)
    monkeypatch.setattr(settings, "database_max_overflow", 3)
    monkeypatch.setattr(settings, "database_pool_timeout", 2.5)
    monkeypatch.setattr(settings, "database_pool_recycle", 900)
    monkeypatch.setattr(settings, "database_pool_pre_ping", False)

    engine = create_db_engine(f"sqlite+aiosqlite:///{tmp_path}/pool.db")
    try:
        assert engine.pool.size() == 12  # type: ignore[attr-defined]
        assert engine.pool._max_overflow == 3  # type: ignore[attr-defined]
        assert engine.pool._timeout == 2.5  # type: ignore[attr-defined]
        assert engine.pool._recycle == 900
        assert engine.pool._pre_ping is False
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


def test_pool_observer_warns_when_connection_is_held_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = SimpleNamespace(info={})
    monotonic = MagicMock(side_effect=[100.0, 112.5])
    monkeypatch.setattr(db.time, "monotonic", monotonic)
    monkeypatch.setattr(db, "_POOL_HOLD_WARN_SECONDS", 10.0)

    with patch.object(db.logger, "warning") as warning:
        db._record_pool_checkout(None, record, None)
        db._record_pool_checkin(None, record)

    warning.assert_called_once()
    assert warning.call_args.kwargs["extra"]["held_seconds"] == 12.5
