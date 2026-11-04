"""Startup import of ``SQLITE_IMPORT_PATH``: when it acts, and when it refuses."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel

from routstr.core import sqlite_import

POSTGRES_URL = "postgresql+asyncpg://routstr:secret@db.invalid:5432/routstr"


def _no_database(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the import must not touch the database")


async def _no_copy(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the import must not copy")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "keys.db"
    path.write_bytes(b"sqlite stand-in")
    return path


def _routstr_sqlite(path: Path, balance: int = 5) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE api_keys (hashed_key TEXT PRIMARY KEY, balance INT)")
    conn.execute("INSERT INTO api_keys VALUES ('k1', ?)", (balance,))
    conn.commit()
    conn.close()
    return path


async def _record_import(target: AsyncEngine, digest: str) -> None:
    async with target.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO database_imports (source_fingerprint, source_path,"
                " imported_at) VALUES (:digest, '/app/keys.db', 1)"
            ),
            {"digest": digest},
        )


@pytest_asyncio.fixture
async def target(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """A migrated-looking target; the decisions before the copy are dialect-free."""
    import routstr.core.db  # noqa: F401  (registers every table)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'target.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_no_setting_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    monkeypatch.delenv("SQLITE_IMPORT_PATH", raising=False)
    monkeypatch.setattr(sqlite_import, "create_async_engine", _no_database)

    await sqlite_import.import_sqlite_on_startup(POSTGRES_URL)

    assert source.exists()


async def test_non_postgres_database_url_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    monkeypatch.setenv("SQLITE_IMPORT_PATH", str(source))
    monkeypatch.setattr(sqlite_import, "create_async_engine", _no_database)

    await sqlite_import.import_sqlite_on_startup("sqlite+aiosqlite:///keys.db")

    assert source.read_bytes() == b"sqlite stand-in"


async def test_import_marker_skips_the_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: AsyncEngine
) -> None:
    monkeypatch.setattr(sqlite_import, "copy_sqlite_to_postgres", _no_copy)
    source = _routstr_sqlite(tmp_path / "keys.db")
    before = source.read_bytes()
    await _record_import(target, sqlite_import._sqlite_digest(source))
    async with target.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO api_keys (hashed_key, balance, reserved_balance,"
                " total_spent, total_requests) VALUES ('k1', 5, 0, 0, 0)"
            )
        )

    await sqlite_import._import_once(target, POSTGRES_URL, source)

    assert source.read_bytes() == before


async def test_imported_file_that_changed_since_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: AsyncEngine
) -> None:
    """A SQLite node kept using the file, so PostgreSQL lacks its new rows."""
    monkeypatch.setattr(sqlite_import, "copy_sqlite_to_postgres", _no_copy)
    source = _routstr_sqlite(tmp_path / "keys.db")
    await _record_import(target, sqlite_import._sqlite_digest(source))
    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO api_keys VALUES ('topped-up-later', 900)")
    conn.commit()
    conn.close()

    with pytest.raises(sqlite_import.MigrationError, match="changed after"):
        await sqlite_import._import_once(target, POSTGRES_URL, source)


async def test_target_with_data_but_no_marker_is_refused(
    monkeypatch: pytest.MonkeyPatch, source: Path, target: AsyncEngine
) -> None:
    monkeypatch.setattr(sqlite_import, "copy_sqlite_to_postgres", _no_copy)
    async with target.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO api_keys (hashed_key, balance, reserved_balance,"
                " total_spent, total_requests) VALUES ('k1', 5, 0, 0, 0)"
            )
        )

    with pytest.raises(sqlite_import.MigrationError, match="refusing to merge"):
        await sqlite_import._import_once(target, POSTGRES_URL, source)

    assert source.read_bytes() == b"sqlite stand-in"


async def test_missing_source_on_an_empty_target_imports_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: AsyncEngine
) -> None:
    monkeypatch.setattr(sqlite_import, "copy_sqlite_to_postgres", _no_copy)

    await sqlite_import._import_once(target, POSTGRES_URL, tmp_path / "absent.db")


async def test_missing_source_keeps_a_fresh_install_starting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: AsyncEngine
) -> None:
    """The first boot of a fresh install writes settings and secrets; the next
    boot still finds no file and must start, not refuse a merge."""
    monkeypatch.setattr(sqlite_import, "copy_sqlite_to_postgres", _no_copy)
    async with target.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO api_keys (hashed_key, balance, reserved_balance,"
                " total_spent, total_requests) VALUES ('k1', 5, 0, 0, 0)"
            )
        )

    await sqlite_import._import_once(target, POSTGRES_URL, tmp_path / "absent.db")


@pytest.mark.parametrize("content", [b"", None])
async def test_file_that_is_not_a_routstr_database_is_refused(
    tmp_path: Path, content: bytes | None
) -> None:
    source = tmp_path / "keys.db"
    if content is None:
        conn = sqlite3.connect(source)
        conn.execute("CREATE TABLE other (x INT)")
        conn.close()
    else:
        source.write_bytes(content)

    with pytest.raises(sqlite_import.MigrationError, match="not a Routstr"):
        await sqlite_import.copy_sqlite_to_postgres(source, POSTGRES_URL)


def test_retired_source_keeps_its_wal(source: Path) -> None:
    wal = source.with_name(source.name + "-wal")
    wal.write_bytes(b"newest commits")

    retired = sqlite_import._retire_source(source)

    assert retired is not None
    assert not source.exists() and not wal.exists()
    assert retired.name.startswith("keys.db.imported-")
    assert retired.read_bytes() == b"sqlite stand-in"
    assert retired.with_name(retired.name + "-wal").read_bytes() == b"newest commits"


def test_read_only_source_is_left_in_place(
    monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    def read_only(self: Path, target: Path) -> Path:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "rename", read_only)

    assert sqlite_import._retire_source(source) is None
    assert source.exists()
