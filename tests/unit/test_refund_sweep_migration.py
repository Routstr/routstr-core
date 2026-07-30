import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _run_alembic(root: Path, database_url: str, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=root,
        env=env,
        check=True,
    )


def test_refund_sweep_repair_restores_column_missing_at_stamped_head(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "missing-refund-sweep-claim.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(root, database_url, "upgrade", "9c4d8e2f1a6b")
    _run_alembic(root, database_url, "stamp", "aa50fde387a2")

    with sqlite3.connect(database_path) as connection:
        columns_before_repair = {
            row[1]
            for row in connection.execute("PRAGMA table_info(cashu_transactions)")
        }
    assert "sweep_started_at" not in columns_before_repair

    _run_alembic(root, database_url, "upgrade", "head")

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        columns_after_repair = {
            row[1]
            for row in connection.execute("PRAGMA table_info(cashu_transactions)")
        }

    assert version == ("b7c9d1e3f5a2",)
    assert "sweep_started_at" in columns_after_repair
