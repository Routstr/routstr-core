import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _run_alembic(root: Path, database_url: str, command: str, revision: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _insert_provider(connection: sqlite3.Connection, slug: str) -> int:
    cursor = connection.execute(
        "INSERT INTO upstream_providers "
        "(slug, provider_type, base_url, api_key, enabled, provider_fee) "
        "VALUES (?, 'custom', ?, ?, 1, 1.01)",
        (slug, f"https://{slug}.example.com", f"key-{slug}"),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_provider_ids_are_not_reused_after_deletion(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "provider-id-migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    previous_head = "ecfa0d6e2a36"

    _run_alembic(root, database_url, "upgrade", previous_head)

    with sqlite3.connect(database_path) as connection:
        assert _insert_provider(connection, "first") == 1
        assert _insert_provider(connection, "second") == 2
        connection.execute("DELETE FROM upstream_providers WHERE id = 2")
        assert _insert_provider(connection, "before-migration") == 2
        connection.commit()

    _run_alembic(root, database_url, "upgrade", "f2a7c9d4e8b1")

    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM upstream_providers WHERE id = 2")
        assert _insert_provider(connection, "after-migration") == 3
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'upstream_providers'"
        ).fetchone()

    assert table_sql is not None
    assert "PRIMARY KEY AUTOINCREMENT" in table_sql[0]


def test_provider_id_migration_downgrades(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "provider-id-downgrade.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(root, database_url, "upgrade", "f2a7c9d4e8b1")
    _run_alembic(root, database_url, "downgrade", "ecfa0d6e2a36")

    with sqlite3.connect(database_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'upstream_providers'"
        ).fetchone()

    assert table_sql is not None
    assert "AUTOINCREMENT" not in table_sql[0]
