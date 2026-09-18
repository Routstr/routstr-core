from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REVISION = "c8e4a1f2b3d5"
PREVIOUS_REVISION = "e4c7a1b9d520"


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


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, tuple[str, int]]:
    return {
        row[1]: (row[2], row[3])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def test_terminal_outcome_migration_round_trips(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "terminal-outcomes.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    ledger_tables = {
        "terminal_outcomes",
        "terminal_outcome_epochs",
        "terminal_outcome_writer_runs",
    }

    _run_alembic(root, database_url, "upgrade", PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert not ledger_tables & _table_names(connection)

    _run_alembic(root, database_url, "upgrade", REVISION)
    with sqlite3.connect(database_path) as connection:
        assert ledger_tables <= _table_names(connection)

        outcome_columns = _columns(connection, "terminal_outcomes")
        assert set(outcome_columns) == {
            "outcome_id",
            "terminal_at_ms",
            "terminal_day",
            "model_identifier",
            "served_model_identifier",
            "pricing_source",
            "input_source",
            "output_source",
            "cache_read_source",
            "cache_creation_source",
            "input_observed",
            "output_observed",
            "cache_read_observed",
            "cache_creation_observed",
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "revenue_msats",
        }
        for column in (
            "terminal_at_ms",
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "revenue_msats",
        ):
            assert outcome_columns[column] == ("BIGINT", 1)
        assert outcome_columns["terminal_day"] == ("DATE", 1)
        assert outcome_columns["model_identifier"] == ("VARCHAR", 0)
        for column in (
            "input_observed",
            "output_observed",
            "cache_read_observed",
            "cache_creation_observed",
        ):
            assert outcome_columns[column] == ("BOOLEAN", 0)
        outcome_index = connection.execute(
            "PRAGMA index_info(ix_terminal_outcomes_terminal_day_terminal_at_ms)"
        ).fetchall()
        assert [row[2] for row in outcome_index] == [
            "terminal_day",
            "terminal_at_ms",
        ]

        epoch_columns = _columns(connection, "terminal_outcome_epochs")
        assert set(epoch_columns) == {
            "epoch",
            "coverage_start_day",
            "coverage_end_day",
            "current_slot",
        }
        assert epoch_columns["epoch"] == ("BIGINT", 1)
        assert epoch_columns["coverage_start_day"] == ("DATE", 1)
        assert epoch_columns["coverage_end_day"] == ("DATE", 0)

        run_columns = _columns(connection, "terminal_outcome_writer_runs")
        assert set(run_columns) == {
            "run_id",
            "status",
            "started_at_ms",
            "heartbeat_at_ms",
            "flushed_through_ms",
            "closed_at_ms",
            "loss_day",
        }
        for column in ("started_at_ms", "heartbeat_at_ms"):
            assert run_columns[column] == ("BIGINT", 1)
        assert run_columns["closed_at_ms"] == ("BIGINT", 0)
        run_index = connection.execute(
            "PRAGMA index_info(ix_terminal_outcome_writer_runs_status_heartbeat)"
        ).fetchall()
        assert [row[2] for row in run_index] == ["status", "heartbeat_at_ms"]

        outcome_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'terminal_outcomes'"
        ).fetchone()
        epoch_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'terminal_outcome_epochs'"
        ).fetchone()
        run_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'terminal_outcome_writer_runs'"
        ).fetchone()
        assert outcome_sql is not None and outcome_sql[0].count("CHECK") == 1
        assert epoch_sql is not None and epoch_sql[0].count("CHECK") == 3
        assert run_sql is not None and run_sql[0].count("CHECK") == 4

        connection.execute(
            "INSERT INTO terminal_outcome_epochs VALUES (0, '2026-09-01', NULL, 1)"
        )
        connection.execute(
            "INSERT INTO terminal_outcome_writer_runs VALUES "
            "('run-1', 'active', 1, 1, NULL, NULL, NULL)"
        )
        connection.execute(
            "INSERT INTO terminal_outcome_writer_runs VALUES "
            "('run-2', 'active', 1, 1, NULL, NULL, NULL)"
        )
        connection.execute(
            "INSERT INTO terminal_outcomes "
            "(outcome_id, terminal_at_ms, terminal_day, model_identifier, "
            "input_observed, output_observed, cache_read_observed, cache_creation_observed, "
            "input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, revenue_msats) VALUES "
            "('request-1', 1, '2026-08-31', 'author/model', "
            "NULL, NULL, NULL, NULL, 10, 5, 0, 0, 1999)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO terminal_outcomes "
                "(outcome_id, terminal_at_ms, terminal_day, model_identifier, "
                "input_observed, output_observed, cache_read_observed, cache_creation_observed, "
                "input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, revenue_msats) VALUES "
                "('request-invalid', 2, '2026-08-31', 'author/model', "
                "NULL, NULL, NULL, NULL, -1, 0, 0, 0, 0)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO terminal_outcome_epochs VALUES (1, '2026-09-02', NULL, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO terminal_outcome_writer_runs VALUES "
                "('run-invalid', 'unknown', 1, 1, NULL, NULL, NULL)"
            )
        connection.commit()

    _run_alembic(root, database_url, "downgrade", PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        tables = _table_names(connection)
        assert not ledger_tables & tables
        assert "api_keys" in tables

    _run_alembic(root, database_url, "upgrade", REVISION)
    with sqlite3.connect(database_path) as connection:
        assert ledger_tables <= _table_names(connection)
        for table in ledger_tables:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
                0,
            )
