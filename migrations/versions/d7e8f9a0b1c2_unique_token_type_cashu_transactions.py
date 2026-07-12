"""Add unique token/type index to Cashu transactions.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_cashu_transactions_token_type"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM cashu_transactions "
            "WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY token, type "
            "      ORDER BY created_at ASC, id ASC"
            "    ) AS row_number "
            "    FROM cashu_transactions"
            "  ) WHERE row_number > 1"
            ")"
        )
    )
    op.create_index(
        _INDEX_NAME,
        "cashu_transactions",
        ["token", "type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="cashu_transactions")
