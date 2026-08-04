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


def _lightning_invoice_columns(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            row[1]
            for row in connection.execute("PRAGMA table_info(lightning_invoices)")
        }


def test_mint_url_migration_upgrades_and_downgrades_from_main_head(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "mint-url-migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    previous_head = "64ed5594df1f"

    _run_alembic(root, database_url, "upgrade", previous_head)
    assert "mint_url" not in _lightning_invoice_columns(database_path)

    _run_alembic(root, database_url, "upgrade", "ecfa0d6e2a36")
    assert "mint_url" in _lightning_invoice_columns(database_path)

    _run_alembic(root, database_url, "downgrade", previous_head)
    assert "mint_url" not in _lightning_invoice_columns(database_path)

    _run_alembic(root, database_url, "upgrade", "head")
    assert "mint_url" in _lightning_invoice_columns(database_path)
