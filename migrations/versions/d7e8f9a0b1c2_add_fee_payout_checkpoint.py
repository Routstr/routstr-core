"""add fee payout checkpoint

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-07-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "routstr_fees",
        sa.Column(
            "payout_in_progress_msats",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "routstr_fees",
        sa.Column("payout_started_at", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("routstr_fees", "payout_started_at")
    op.drop_column("routstr_fees", "payout_in_progress_msats")
