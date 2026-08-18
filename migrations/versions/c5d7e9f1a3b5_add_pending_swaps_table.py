"""add pending_swaps table

Revision ID: c5d7e9f1a3b5
Revises: b4f7a1c9d2e3
Create Date: 2026-08-18 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c5d7e9f1a3b5"
down_revision = "b4f7a1c9d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_swaps",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("source_mint", sa.String(), nullable=False),
        sa.Column("source_unit", sa.String(), nullable=False),
        sa.Column("melt_quote_id", sa.String(), nullable=False),
        sa.Column("dest_mint", sa.String(), nullable=False),
        sa.Column("dest_unit", sa.String(), nullable=False),
        sa.Column("mint_quote_id", sa.String(), nullable=False),
        sa.Column("minted_amount", sa.Integer(), nullable=False),
        sa.Column("key_hashed_key", sa.String(), nullable=True),
        sa.Column("token", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_pending_swaps_key_hashed_key", "pending_swaps", ["key_hashed_key"]
    )
    op.create_index("ix_pending_swaps_state", "pending_swaps", ["state"])


def downgrade() -> None:
    op.drop_index("ix_pending_swaps_state", table_name="pending_swaps")
    op.drop_index("ix_pending_swaps_key_hashed_key", table_name="pending_swaps")
    op.drop_table("pending_swaps")
