"""repair refund sweep claim lease

Revision ID: b7c9d1e3f5a2
Revises: aa50fde387a2
Create Date: 2026-07-30 03:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c9d1e3f5a2"
down_revision = "aa50fde387a2"
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
    # Revision aa50fde387a2 owns this column, so downgrading only the repair
    # migration must preserve the schema expected at that revision.
    pass
