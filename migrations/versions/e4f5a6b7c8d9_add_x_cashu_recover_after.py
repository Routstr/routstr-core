"""add x-cashu recovery deadline and refund amount

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
    columns = _columns()
    with op.batch_alter_table("cashu_transactions") as batch_op:
        # Existing rows stay NULL and keep their previous fallback behaviour.
        if "recover_after" not in columns:
            batch_op.add_column(
                sa.Column("recover_after", sa.BigInteger(), nullable=True)
            )
        if "refund_amount" not in columns:
            batch_op.add_column(
                sa.Column("refund_amount", sa.BigInteger(), nullable=True)
            )


def downgrade() -> None:
    columns = _columns()
    with op.batch_alter_table("cashu_transactions") as batch_op:
        if "refund_amount" in columns:
            batch_op.drop_column("refund_amount")
        if "recover_after" in columns:
            batch_op.drop_column("recover_after")
