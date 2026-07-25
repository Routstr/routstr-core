"""repair missing fee payout checkpoint columns

Revision ID: 9c4d8e2f1a6b
Revises: 7f2843d3f4e4
Create Date: 2026-07-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c4d8e2f1a6b"
down_revision = "7f2843d3f4e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Repair databases stamped past the original checkpoint migration."""
    conn = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(conn).get_columns("routstr_fees")
    }

    if "payout_in_progress_msats" not in columns:
        op.add_column(
            "routstr_fees",
            sa.Column(
                "payout_in_progress_msats",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )

    if "payout_started_at" not in columns:
        op.add_column(
            "routstr_fees",
            sa.Column("payout_started_at", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    # The preceding revision already expects both columns. This migration only
    # repairs schema drift, so downgrading it must preserve the expected schema.
    pass
