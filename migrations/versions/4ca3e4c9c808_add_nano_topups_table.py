"""add nano_topups table

Revision ID: 4ca3e4c9c808
Revises: c8a1d2e3f4b5
Create Date: 2026-10-24

"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "4ca3e4c9c808"
down_revision = "c8a1d2e3f4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nano_topups",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("api_key_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("amount_sats", sa.Integer(), nullable=False),
        sa.Column("amount_raw", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("raw_tag", sa.Integer(), nullable=False),
        sa.Column("address", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("block_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("paid_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nano_topups_api_key_hash", "nano_topups", ["api_key_hash"])
    op.create_index("ix_nano_topups_raw_tag", "nano_topups", ["raw_tag"])
    op.create_index("ix_nano_topups_status", "nano_topups", ["status"])
    op.create_index(
        "ix_nano_topups_block_hash", "nano_topups", ["block_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_nano_topups_block_hash", table_name="nano_topups")
    op.drop_index("ix_nano_topups_status", table_name="nano_topups")
    op.drop_index("ix_nano_topups_raw_tag", table_name="nano_topups")
    op.drop_index("ix_nano_topups_api_key_hash", table_name="nano_topups")
    op.drop_table("nano_topups")
