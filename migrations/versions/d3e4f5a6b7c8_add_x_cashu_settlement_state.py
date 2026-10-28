"""add x-cashu settlement state to cashu_transactions

Revision ID: d3e4f5a6b7c8
Revises: c8a1d2e3f4b5
Create Date: 2026-10-26 00:00:00.000000
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "d3e4f5a6b7c8"
down_revision = "c8a1d2e3f4b5"
branch_labels = None
depends_on = None

INDEX = "ix_cashu_transactions_settlement"


def _columns() -> set[str]:
    conn = op.get_bind()
    return {
        column["name"] for column in sa.inspect(conn).get_columns("cashu_transactions")
    }


def _indexes() -> set[str]:
    conn = op.get_bind()
    return {
        index["name"]
        for index in sa.inspect(conn).get_indexes("cashu_transactions")
        if index["name"]
    }


def upgrade() -> None:
    # Existing rows stay NULL: their outcome was never recorded, so they must
    # not be mistaken for unsettled payments and refunded.
    columns = _columns()
    with op.batch_alter_table("cashu_transactions") as batch_op:
        if "settlement" not in columns:
            batch_op.add_column(
                sa.Column(
                    "settlement", sqlmodel.sql.sqltypes.AutoString(), nullable=True
                )
            )
        if "settlement_at" not in columns:
            batch_op.add_column(sa.Column("settlement_at", sa.Integer(), nullable=True))
    if INDEX not in _indexes():
        op.create_index(
            INDEX, "cashu_transactions", ["settlement", "settlement_at"], unique=False
        )


def downgrade() -> None:
    if INDEX in _indexes():
        op.drop_index(INDEX, table_name="cashu_transactions")
    columns = _columns()
    with op.batch_alter_table("cashu_transactions") as batch_op:
        if "settlement_at" in columns:
            batch_op.drop_column("settlement_at")
        if "settlement" in columns:
            batch_op.drop_column("settlement")
