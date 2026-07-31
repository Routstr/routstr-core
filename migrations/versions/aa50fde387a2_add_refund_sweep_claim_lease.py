"""add refund sweep claim lease

Revision ID: aa50fde387a2
Revises: 9c4d8e2f1a6b
Create Date: 2026-07-26 12:50:10.509217
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "aa50fde387a2"
down_revision = "9c4d8e2f1a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(conn).get_columns("cashu_transactions")
    }
    if "sweep_started_at" not in columns:
        op.add_column(
            "cashu_transactions",
            sa.Column("sweep_started_at", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(conn).get_columns("cashu_transactions")
    }
    if "sweep_started_at" in columns:
        op.drop_column("cashu_transactions", "sweep_started_at")
