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


def test_fresh_node_migrates_fee_payout_schema_to_head(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "fresh-node.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(root, database_url, "head")

    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(routstr_fees)")
        }
        fee = connection.execute(
            "SELECT id, accumulated_msats, total_paid_msats, last_paid_at, "
            "payout_in_progress_msats, payout_started_at FROM routstr_fees"
        ).fetchone()

    assert version == ("c7d5f8638599",)
    assert {
        "id",
        "accumulated_msats",
        "total_paid_msats",
        "last_paid_at",
        "payout_in_progress_msats",
        "payout_started_at",
    } <= columns
    assert fee == (1, 0, 0, None, 0, None)


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


def test_fee_payout_checkpoint_repair_restores_columns_missing_at_old_head(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    old_head = "7f2843d3f4e4"
    _run_alembic(root, database_url, old_head)

    # Reproduce a database that was stamped to head after a duplicate-column or
    # unknown-revision recovery skipped part of the migration chain.
    with sqlite3.connect(database_path) as connection:
        connection.execute("ALTER TABLE routstr_fees DROP COLUMN payout_started_at")
        connection.execute(
            "ALTER TABLE routstr_fees DROP COLUMN payout_in_progress_msats"
        )
        connection.commit()

    _run_alembic(root, database_url, "head")

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(routstr_fees)")
        }
        row = connection.execute(
            "SELECT payout_in_progress_msats, payout_started_at "
            "FROM routstr_fees WHERE id = 1"
        ).fetchone()

    assert {"payout_in_progress_msats", "payout_started_at"} <= columns
    assert row == (0, None)
