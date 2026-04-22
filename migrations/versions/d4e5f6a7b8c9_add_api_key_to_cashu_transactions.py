"""add api key link to cashu_transactions

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-20 00:00:00.000000
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("cashu_transactions")]
    indexes = {index["name"] for index in inspector.get_indexes("cashu_transactions")}

    if "api_key_hashed_key" not in columns:
        op.add_column(
            "cashu_transactions",
            sa.Column(
                "api_key_hashed_key",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=True,
            ),
        )

    if "ix_cashu_transactions_api_key_hashed_key" not in indexes:
        op.create_index(
            "ix_cashu_transactions_api_key_hashed_key",
            "cashu_transactions",
            ["api_key_hashed_key"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_cashu_transactions_api_key_hashed_key", table_name="cashu_transactions")
    op.drop_column("cashu_transactions", "api_key_hashed_key")
