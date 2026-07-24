from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from routstr.core import db
from routstr.core.db import _engine_options


def test_engine_options_use_bounded_fail_fast_pool(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DATABASE_POOL_SIZE", raising=False)
    monkeypatch.delenv("DATABASE_MAX_OVERFLOW", raising=False)
    monkeypatch.delenv("DATABASE_POOL_TIMEOUT", raising=False)
    monkeypatch.delenv("DATABASE_POOL_RECYCLE", raising=False)
    monkeypatch.delenv("DATABASE_POOL_PRE_PING", raising=False)

    options = _engine_options("postgresql+asyncpg://db/routstr")

    assert options == {
        "pool_size": 5,
        "max_overflow": 0,
        "pool_timeout": 5.0,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    }


def test_engine_options_are_operator_configurable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATABASE_POOL_SIZE", "12")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "3")
    monkeypatch.setenv("DATABASE_POOL_TIMEOUT", "2.5")
    monkeypatch.setenv("DATABASE_POOL_RECYCLE", "900")
    monkeypatch.setenv("DATABASE_POOL_PRE_PING", "false")

    assert _engine_options("sqlite+aiosqlite:///keys.db") == {
        "pool_size": 12,
        "max_overflow": 3,
        "pool_timeout": 2.5,
        "pool_recycle": 900,
        "pool_pre_ping": False,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_POOL_SIZE", "0"),
        ("DATABASE_MAX_OVERFLOW", "-1"),
        ("DATABASE_POOL_TIMEOUT", "0"),
        ("DATABASE_POOL_RECYCLE", "-1"),
        ("DATABASE_POOL_PRE_PING", "maybe"),
    ],
)
def test_engine_options_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        _engine_options("postgresql+asyncpg://db/routstr")


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


def test_memory_sqlite_keeps_dialect_static_pool(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DATABASE_POOL_PRE_PING", raising=False)

    assert _engine_options("sqlite+aiosqlite://") == {"pool_pre_ping": True}
