import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def test_alembic_cli_accepts_a_percent_in_the_database_url(tmp_path: Path) -> None:
    # %-encoded passwords and socket paths put "%" in the URL, which
    # configparser would otherwise read as interpolation syntax.
    directory = tmp_path / "pct%dir"
    directory.mkdir()
    database_path = directory / "keys.db"

    result = _run(
        f"sqlite+aiosqlite:///{database_path}", "-m", "alembic", "upgrade", "head"
    )

    assert result.returncode == 0, result.stderr
    assert "alembic_version" in _tables(database_path)


def test_startup_migrations_accept_a_percent_in_the_database_url(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "pct%dir"
    directory.mkdir()
    database_path = directory / "keys.db"

    result = _run(
        f"sqlite+aiosqlite:///{database_path}",
        "-c",
        "from routstr.core.db import run_migrations; run_migrations()",
    )

    assert result.returncode == 0, result.stderr
    assert "alembic_version" in _tables(database_path)
