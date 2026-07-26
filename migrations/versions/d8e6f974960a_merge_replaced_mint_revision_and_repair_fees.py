"""merge replaced mint revision and repair fee checkpoint

Revision ID: d8e6f974960a
Revises: c7d5f8638599, 11eaab843b49, 21c84cd5ad83
Create Date: 2026-07-26 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8e6f974960a"
down_revision = ("c7d5f8638599", "11eaab843b49", "21c84cd5ad83")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Repair nodes previously stamped past the fee checkpoint migration."""
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("routstr_fees")
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
    # Both parent branches expect these columns when their histories are intact.
    pass
