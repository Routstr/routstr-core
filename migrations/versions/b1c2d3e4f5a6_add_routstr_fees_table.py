"""add routstr_fees table

Revision ID: b1c2d3e4f5a6
Revises: a776ca70e5fe
Create Date: 2026-03-20 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b1c2d3e4f5a6"
down_revision = "a776ca70e5fe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "routstr_fees",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("accumulated_msats", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_paid_msats", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_paid_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Seed with a single row
    op.execute("INSERT INTO routstr_fees (id, accumulated_msats, total_paid_msats) VALUES (1, 0, 0)")


def downgrade() -> None:
    op.drop_table("routstr_fees")
