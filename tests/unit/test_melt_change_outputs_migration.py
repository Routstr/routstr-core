import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_alembic(database_url: str, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _columns(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            row[1]
            for row in connection.execute("PRAGMA table_info(melt_change_outputs)")
        }


def test_melt_change_outputs_migration_upgrades_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "melt-change.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(database_url, "upgrade", "a611996b478e")
    assert _columns(database_path) == {
        "quote_id",
        "mint_url",
        "unit",
        "keyset_id",
        "counter",
        "count",
        "created_at",
    }

    _run_alembic(database_url, "downgrade", "e4f5a6b7c8d9")
    assert _columns(database_path) == set()

    _run_alembic(database_url, "upgrade", "head")
    assert "quote_id" in _columns(database_path)
