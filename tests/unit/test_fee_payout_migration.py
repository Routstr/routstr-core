import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _run_alembic(root: Path, database_url: str, revision: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_fee_payout_checkpoint_migration_preserves_existing_row(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    _run_alembic(root, database_url, "c6d7e8f9a0b1")

    with sqlite3.connect(database_path) as connection:
        result = connection.execute(
            "UPDATE routstr_fees SET accumulated_msats = 5000, "
            "total_paid_msats = 1000, last_paid_at = 123 WHERE id = 1"
        )
        assert result.rowcount == 1
        connection.commit()

    _run_alembic(root, database_url, "head")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT accumulated_msats, total_paid_msats, last_paid_at, "
            "payout_in_progress_msats, payout_started_at "
            "FROM routstr_fees WHERE id = 1"
        ).fetchone()

    assert row == (5000, 1000, 123, 0, None)
