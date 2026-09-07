"""add refunds table

Revision ID: f3a1c7b9e2d4
Revises: e5a6b7c8d9f0
Create Date: 2026-09-07

"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "f3a1c7b9e2d4"
down_revision = "e5a6b7c8d9f0"
branch_labels = None
depends_on = None

OPEN_STATUSES = "status IN ('pending', 'ambiguous')"


def upgrade() -> None:
    op.create_table(
        "refunds",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "api_key_hashed_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column("method", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("destination", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("amount_msats", sa.Integer(), nullable=False),
        sa.Column("unit", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("mint_url", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("quote_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("token", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("claimed_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["api_key_hashed_key"], ["api_keys.hashed_key"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refunds_api_key_hashed_key", "refunds", ["api_key_hashed_key"])
    op.create_index("ix_refunds_status", "refunds", ["status"])
    op.create_index(
        "ux_refunds_open_per_key",
        "refunds",
        ["api_key_hashed_key"],
        unique=True,
        sqlite_where=sa.text(OPEN_STATUSES),
        postgresql_where=sa.text(OPEN_STATUSES),
    )


def downgrade() -> None:
    op.drop_index("ux_refunds_open_per_key", table_name="refunds")
    op.drop_index("ix_refunds_status", table_name="refunds")
    op.drop_index("ix_refunds_api_key_hashed_key", table_name="refunds")
    op.drop_table("refunds")
