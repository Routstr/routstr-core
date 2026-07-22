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


def test_disables_enabled_unchargeable_rows_only() -> None:
    """Legacy rows that motivated the change — enabled but priced at nothing —
    are fail-closed by the migration itself, not only when re-saved through the
    admin endpoint. Chargeable rows (including per-request-billed ones) and
    already-disabled rows are left untouched."""
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE models ("
                "id VARCHAR, upstream_provider_id INTEGER, "
                "pricing VARCHAR, enabled BOOLEAN, "
                "PRIMARY KEY (id, upstream_provider_id))"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO models "
                "(id, upstream_provider_id, pricing, enabled) VALUES "
                "('free-enabled', 1, '{\"prompt\": 0, \"completion\": 0}', 1), "
                "('priced-enabled', 1, "
                "'{\"prompt\": 0.000001, \"completion\": 0}', 1), "
                "('request-priced', 1, "
                "'{\"prompt\": 0, \"completion\": 0, \"request\": 0.5}', 1), "
                "('free-disabled', 1, '{\"prompt\": 0, \"completion\": 0}', 0), "
                "('junk-pricing', 1, 'not-json', 1)"
            )
        )

        migration._disable_unchargeable_enabled_rows(conn)

        rows: dict[str, int] = {
            r[0]: r[1]
            for r in conn.execute(sa.text("SELECT id, enabled FROM models")).all()
        }

    assert rows["free-enabled"] == 0  # unchargeable + enabled → disabled
    assert rows["priced-enabled"] == 1  # chargeable → untouched
    assert rows["request-priced"] == 1  # per-request billed is chargeable
    assert rows["free-disabled"] == 0  # already disabled → stays
    assert rows["junk-pricing"] == 0  # unparseable price fails closed
