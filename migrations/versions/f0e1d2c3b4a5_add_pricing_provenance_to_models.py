"""add pricing provenance to models and fail-close unchargeable rows

Revision ID: f0e1d2c3b4a5
Revises: c6d7e8f9a0b1
Create Date: 2026-07-16 00:00:00.000000
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "f0e1d2c3b4a5"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None

# Frozen snapshot of routstr.payment.models.BILLABLE_PRICING_FIELDS — the rates
# whose positive value makes a request chargeable. Copied (not imported) so the
# migration stays stable if the app's field set later changes.
_BILLABLE_FIELDS = (
    "prompt",
    "completion",
    "request",
    "image",
    "web_search",
    "internal_reasoning",
    "input_cache_read",
    "input_cache_write",
)


def _backfill_pricing_source(conn: sa.Connection) -> None:
    # Existing rows predate provenance tracking, so their true origin is
    # unknowable. Backfill "unresolved" rather than assert a trusted source:
    # a persisted fail-closed zero-price row relabelled "openrouter" would slip
    # past the re-enable guard.
    conn.execute(
        sa.text(
            "UPDATE models SET pricing_source = 'unresolved' "
            "WHERE pricing_source IS NULL"
        )
    )


def _row_is_chargeable(pricing_json: object) -> bool:
    """True if the stored pricing JSON has any positive billable rate.

    Unparseable / missing pricing fails closed (treated as unchargeable), the
    safe direction for a money guard.
    """
    if not isinstance(pricing_json, str):
        return False
    try:
        pricing = json.loads(pricing_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(pricing, dict):
        return False
    for field in _BILLABLE_FIELDS:
        try:
            if float(pricing.get(field, 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _disable_unchargeable_enabled_rows(conn: sa.Connection) -> None:
    # The legacy rows that motivated provenance: an enabled model priced at
    # nothing bills every request at zero. list_models() gates only on
    # `enabled`, so fail-close them here rather than wait for an admin re-save.
    rows = conn.execute(
        sa.text("SELECT rowid, pricing FROM models WHERE enabled")
    ).all()
    stale = [rowid for rowid, pricing_json in rows if not _row_is_chargeable(pricing_json)]
    if not stale:
        return
    conn.execute(
        sa.text("UPDATE models SET enabled = 0 WHERE rowid IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)
        ),
        {"ids": stale},
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("models")}

    if "pricing_source" not in columns:
        op.add_column(
            "models",
            sa.Column("pricing_source", sa.String(), nullable=True),
        )

    _backfill_pricing_source(conn)
    _disable_unchargeable_enabled_rows(conn)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("models")}

    if "pricing_source" in columns:
        op.drop_column("models", "pricing_source")
