"""Migration test for the pricing-provenance backfill.

Pre-provenance rows have an unknowable origin, so the backfill assigns
``unresolved`` (not a trusted source) — the only value that keeps the
re-enable guard sound for a persisted fail-closed zero-price row. Rows that
somehow already carry a source are left untouched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "f0e1d2c3b4a5_add_pricing_provenance_to_models.py"
)
_spec = importlib.util.spec_from_file_location("pricing_provenance_migration", _MIGRATION_PATH)
assert _spec is not None and _spec.loader is not None
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


def test_backfill_assigns_unresolved_and_preserves_existing() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE models ("
                "id VARCHAR PRIMARY KEY, "
                "pricing_source VARCHAR NULL"
                ")"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO models (id, pricing_source) VALUES "
                "('a', NULL), "
                "('b', 'manual')"
            )
        )

        migration._backfill_pricing_source(conn)

        rows = conn.execute(
            sa.text("SELECT id, pricing_source FROM models ORDER BY id")
        ).all()

    assert rows == [("a", "unresolved"), ("b", "manual")]
