"""add reservation release idempotency records

Revision ID: f9a0b1c2d3e4
Revises: d7e8f9a0b1c2
Create Date: 2026-07-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f9a0b1c2d3e4"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reservation_releases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("billing_key_hash", sa.String(), nullable=False),
        sa.Column("reserved_msats", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reservation_releases_key_hash",
        "reservation_releases",
        ["key_hash"],
    )
    op.create_index(
        "ix_reservation_releases_billing_key_hash",
        "reservation_releases",
        ["billing_key_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reservation_releases_billing_key_hash",
        table_name="reservation_releases",
    )
    op.drop_index(
        "ix_reservation_releases_key_hash",
        table_name="reservation_releases",
    )
    op.drop_table("reservation_releases")
