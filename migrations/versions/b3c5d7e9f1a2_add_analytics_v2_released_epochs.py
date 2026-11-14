"""add operator-released analytics v2 epochs

Revision ID: b3c5d7e9f1a2
Revises: 98cf337f18f4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b3c5d7e9f1a2"
down_revision = "98cf337f18f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_v2_released_epochs",
        sa.Column("epoch", sa.BigInteger(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("released_at_ms", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("analytics_v2_released_epochs")
