"""SQLite values the data migration must turn into what PostgreSQL binds."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa

SCRIPT = Path(__file__).parents[2] / "scripts" / "migrate_sqlite_to_postgres.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_sqlite_to_postgres", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


migrate = _script()


def _column(type_: sa.types.TypeEngine[Any]) -> sa.Column[Any]:
    return sa.Column("value", type_)


def test_date_stored_as_text_binds_as_a_date() -> None:
    column = _column(sa.Date())

    assert migrate._coerce("2026-10-25", column, "t.value") == date(2026, 10, 25)
    assert migrate._coerce(date(2026, 10, 25), column, "t.value") == date(2026, 10, 25)
    with pytest.raises(migrate.MigrationError, match="is not a date"):
        migrate._coerce("not-a-day", column, "t.value")


def test_binary_column_keeps_its_bytes() -> None:
    column = _column(sa.LargeBinary())
    frame = b"\x00\xff\x80frame"

    assert migrate._coerce(frame, column, "t.value") == frame
    assert migrate._coerce(memoryview(frame), column, "t.value") == frame
    with pytest.raises(migrate.MigrationError, match="is not binary data"):
        migrate._coerce("frame", column, "t.value")


def test_text_column_still_decodes_utf8_bytes() -> None:
    assert migrate._coerce(b"node", _column(sa.String()), "t.value") == "node"
