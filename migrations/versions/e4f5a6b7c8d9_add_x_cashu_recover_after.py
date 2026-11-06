"""add x-cashu recovery deadline to cashu_transactions

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-11-06 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    conn = op.get_bind()
    return {
        column["name"] for column in sa.inspect(conn).get_columns("cashu_transactions")
    }


def upgrade() -> None:
    # Existing rows stay NULL and fall back to the current request lifetime.
    if "recover_after" not in _columns():
        with op.batch_alter_table("cashu_transactions") as batch_op:
            batch_op.add_column(
                sa.Column("recover_after", sa.BigInteger(), nullable=True)
            )


def downgrade() -> None:
    if "recover_after" in _columns():
        with op.batch_alter_table("cashu_transactions") as batch_op:
            batch_op.drop_column("recover_after")
