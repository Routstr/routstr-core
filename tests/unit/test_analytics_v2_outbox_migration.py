from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REVISION = "d9a6e2f4c7b1"
PREVIOUS_REVISION = "c8e4a1f2b3d5"


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


def _tables(connection: sqlite3.Connection) -> set[str]:
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


def test_analytics_v2_outbox_migration_round_trips(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "analytics-v2-outbox.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    tables = {
        "analytics_v2_delivery_state",
        "analytics_v2_outbox",
        "analytics_v2_relay_receipts",
    }

    _run_alembic(root, database_url, "upgrade", PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert not tables & _tables(connection)

    _run_alembic(root, database_url, "upgrade", REVISION)
    with sqlite3.connect(database_path) as connection:
        assert tables <= _tables(connection)
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
            for table in tables
        )

        outbox_columns = _columns(connection, "analytics_v2_outbox")
        assert outbox_columns["frame"] == ("BLOB", 1)
        for column in (
            "epoch",
            "delivery_generation",
            "created_at",
            "stored_at_ms",
            "next_attempt_at_ms",
            "attempt_count",
        ):
            assert outbox_columns[column] == ("BIGINT", 1)
        assert outbox_columns["through_day"] == ("DATE", 1)
        assert outbox_columns["semantic_slot"] == ("VARCHAR", 1)
        assert outbox_columns["first_send_attempt_at_ms"] == ("BIGINT", 0)
        assert "acknowledged_at_ms" not in outbox_columns
        assert outbox_columns["delivered_at_ms"] == ("BIGINT", 0)
        state_columns = _columns(connection, "analytics_v2_delivery_state")
        assert state_columns["active_epoch_floor"] == ("BIGINT", 0)
        assert not {"ever_send_attempted", "ever_acknowledged"} & state_columns.keys()

        pending_index = connection.execute(
            "PRAGMA index_info(ix_analytics_v2_outbox_pending)"
        ).fetchall()
        assert [row[2] for row in pending_index] == [
            "status",
            "delivery_generation",
            "next_attempt_at_ms",
        ]
        coordinate_index = connection.execute(
            "PRAGMA index_info(ix_analytics_v2_outbox_coordinate)"
        ).fetchall()
        assert [row[2] for row in coordinate_index] == [
            "pubkey",
            "d_tag",
            "epoch",
            "created_at",
        ]

        connection.execute(
            "INSERT INTO analytics_v2_delivery_state VALUES "
            "(1, 1, 1, 0, ?, 'provider', 1)",
            ("11" * 32,),
        )
        connection.execute(
            "INSERT INTO analytics_v2_outbox VALUES "
            "(?, ?, 'routstr.analytics.v2:test:week:2026-08-31', 38422, "
            "'2026-08-31', 1, '2026-08-31', "
            "'ordinary:1:2026-08-31', 1, X'5B5D', 0, 0, 'pending', "
            "1, 1, 1, 0, NULL, NULL)",
            ("33" * 32, "11" * 32),
        )
        connection.execute(
            "INSERT INTO analytics_v2_relay_receipts VALUES "
            "(?, 'wss://relay.example.net', 1, 1)",
            ("33" * 32,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO analytics_v2_delivery_state VALUES "
                "(2, 0, 0, NULL, NULL, NULL, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE analytics_v2_delivery_state "
                "SET active_epoch_floor = NULL WHERE id = 1"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO analytics_v2_outbox VALUES "
                "(?, ?, 'invalid', 1, '2026-08-31', 0, '2026-08-31', "
                "'ordinary:0:2026-08-31', 0, X'5B5D', "
                "0, 0, 'pending', 1, 1, 1, 0, NULL, NULL)",
                ("44" * 32, "11" * 32),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO analytics_v2_outbox VALUES "
                "(?, ?, 'routstr.analytics.v2:test:week:2026-08-31', 38422, "
                "'2026-08-31', 1, '2026-08-31', "
                "'ordinary:1:2026-08-31', 1, X'5B5D', "
                "0, 0, 'pending', 2, 2, 2, 0, NULL, NULL)",
                ("55" * 32, "11" * 32),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO analytics_v2_relay_receipts VALUES "
                "(?, 'wss://other.example.net', -1, 1)",
                ("33" * 32,),
            )
        connection.commit()

    _run_alembic(root, database_url, "downgrade", PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert not tables & _tables(connection)
        assert "terminal_outcomes" in _tables(connection)

    _run_alembic(root, database_url, "upgrade", REVISION)
    with sqlite3.connect(database_path) as connection:
        assert tables <= _tables(connection)
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
            for table in tables
        )
