"""add melt change outputs table

Revision ID: a611996b478e
Revises: e4f5a6b7c8d9
Create Date: 2026-11-10 00:00:00.000000

A melt that settles after its own reply (pending, timed out, or lost) is
checked later with cashu's get_melt_quote, which does not unblind the fee
reserve change. Recording the blank outputs' counter range lets that later
check store the change proofs.
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "a611996b478e"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An older node re-stamps an unknown head on SQLite and keeps the table.
    if sa.inspect(op.get_bind()).has_table("melt_change_outputs"):
        return
    op.create_table(
        "melt_change_outputs",
        sa.Column("quote_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("mint_url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("unit", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("keyset_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("counter", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("quote_id"),
    )


def downgrade() -> None:
    op.drop_table("melt_change_outputs")
