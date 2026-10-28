import os
import sqlite3
import subprocess
import sys
from pathlib import Path

PREVIOUS_HEAD = "c8a1d2e3f4b5"


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


def _columns(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            row[1]
            for row in connection.execute("PRAGMA table_info(cashu_transactions)")
        }


def test_settlement_migration_leaves_existing_payments_untracked(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "settlement-migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(root, database_url, "upgrade", PREVIOUS_HEAD)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO cashu_transactions (id, token, amount, unit, type, "
            "request_id, created_at, collected, swept, source) "
            "VALUES ('legacy', 'cashuAold', 20, 'sat', 'in', 'req-old', 1, 1, 0, "
            "'x-cashu')"
        )

    _run_alembic(root, database_url, "upgrade", "head")
    assert {"settlement", "settlement_at"} <= _columns(database_path)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT settlement, settlement_at FROM cashu_transactions "
            "WHERE id = 'legacy'"
        ).fetchone()
    # Unknown outcomes stay NULL so recovery never refunds them.
    assert row == (None, None)

    _run_alembic(root, database_url, "downgrade", PREVIOUS_HEAD)
    assert not {"settlement", "settlement_at"} & _columns(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT token FROM cashu_transactions WHERE id = 'legacy'"
        ).fetchone() == ("cashuAold",)

    _run_alembic(root, database_url, "upgrade", "head")
    assert {"settlement", "settlement_at"} <= _columns(database_path)
