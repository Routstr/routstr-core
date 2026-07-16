"""add pricing provenance columns to models

Revision ID: f0e1d2c3b4a5
Revises: c6d7e8f9a0b1
Create Date: 2026-07-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f0e1d2c3b4a5"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def _backfill_pricing_source(conn: sa.Connection) -> None:
    # Existing rows predate provenance tracking, so their true origin is
    # unknowable. Backfill "unresolved" rather than assert a trusted source:
    # a persisted fail-closed zero-price row relabelled "openrouter" would slip
    # past the re-enable guard. Freshness columns stay NULL — we never resolved
    # these prices ourselves.
    conn.execute(
        sa.text(
            "UPDATE models SET pricing_source = 'unresolved' "
            "WHERE pricing_source IS NULL"
        )
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
    if "pricing_checked_at" not in columns:
        op.add_column(
            "models",
            sa.Column("pricing_checked_at", sa.Integer(), nullable=True),
        )
    if "pricing_source_version" not in columns:
        op.add_column(
            "models",
            sa.Column("pricing_source_version", sa.String(), nullable=True),
        )

    _backfill_pricing_source(conn)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("models")}

    for column in (
        "pricing_source_version",
        "pricing_checked_at",
        "pricing_source",
    ):
        if column in columns:
            op.drop_column("models", column)
