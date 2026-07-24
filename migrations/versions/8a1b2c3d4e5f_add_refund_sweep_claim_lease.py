"""add recoverable refund sweep claim lease

Revision ID: 8a1b2c3d4e5f
Revises: 7f2843d3f4e4
Create Date: 2026-07-24 23:15:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "8a1b2c3d4e5f"
down_revision = "7f2843d3f4e4"
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
