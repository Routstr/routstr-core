"""add key management and reset fields to api_keys

Revision ID: 06f81c0fc88d
Revises: c2d3e4f5a6b7
Create Date: 2026-02-04 22:44:03.311983
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "06f81c0fc88d"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("balance_limit", sa.Integer(), nullable=True))
    op.add_column(
        "api_keys",
        sa.Column(
            "balance_limit_reset", sqlmodel.sql.sqltypes.AutoString(), nullable=True
        ),
    )
    op.add_column(
        "api_keys", sa.Column("balance_limit_reset_date", sa.Integer(), nullable=True)
    )
    op.add_column("api_keys", sa.Column("validity_date", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "validity_date")
    op.drop_column("api_keys", "balance_limit_reset_date")
    op.drop_column("api_keys", "balance_limit_reset")
    op.drop_column("api_keys", "balance_limit")
