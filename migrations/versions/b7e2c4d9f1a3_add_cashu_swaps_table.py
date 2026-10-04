"""add cashu_swaps table

Revision ID: b7e2c4d9f1a3
Revises: a73d19b6c204
Create Date: 2026-10-04

"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "b7e2c4d9f1a3"
down_revision = "a73d19b6c204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cashu_swaps",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("direction", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "api_key_hashed_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True
        ),
        sa.Column("refund_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("source_mint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_unit", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_amount", sa.Integer(), nullable=False),
        sa.Column(
            "destination_mint", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column(
            "destination_unit", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column("destination_amount", sa.Integer(), nullable=False),
        sa.Column("fee_reserve", sa.Integer(), nullable=False),
        sa.Column("input_fees", sa.Integer(), nullable=False),
        sa.Column("mint_quote_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("melt_quote_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("token", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("claimed_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["api_key_hashed_key"], ["api_keys.hashed_key"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cashu_swaps_status", "cashu_swaps", ["status"])
    op.create_index(
        "ix_cashu_swaps_api_key_hashed_key", "cashu_swaps", ["api_key_hashed_key"]
    )
    op.create_index("ix_cashu_swaps_refund_id", "cashu_swaps", ["refund_id"])
    op.create_index(
        "ix_cashu_swaps_token_hash", "cashu_swaps", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_cashu_swaps_token_hash", table_name="cashu_swaps")
    op.drop_index("ix_cashu_swaps_refund_id", table_name="cashu_swaps")
    op.drop_index("ix_cashu_swaps_api_key_hashed_key", table_name="cashu_swaps")
    op.drop_index("ix_cashu_swaps_status", table_name="cashu_swaps")
    op.drop_table("cashu_swaps")
