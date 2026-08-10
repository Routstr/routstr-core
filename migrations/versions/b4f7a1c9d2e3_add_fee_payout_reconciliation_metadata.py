"""add fee payout reconciliation metadata

Revision ID: b4f7a1c9d2e3
Revises: f2a7c9d4e8b1
Create Date: 2026-08-11 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b4f7a1c9d2e3"
down_revision = "f2a7c9d4e8b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "routstr_fees",
        sa.Column("payout_quote_id", sa.String(), nullable=True),
    )
    op.add_column(
        "routstr_fees",
        sa.Column("payout_mint_url", sa.String(), nullable=True),
    )
    op.add_column(
        "routstr_fees",
        sa.Column("payout_unit", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("routstr_fees", "payout_unit")
    op.drop_column("routstr_fees", "payout_mint_url")
    op.drop_column("routstr_fees", "payout_quote_id")
