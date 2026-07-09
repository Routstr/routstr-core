"""unique (token, type) constraint on cashu_transactions

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-07-10 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


_CONSTRAINT_NAME = "uq_cashu_transactions_token_type"


def _dedup_rows(conn: sa.Connection) -> None:
    """Delete duplicate (token, type) rows, keeping the oldest per group."""
    conn.execute(
        sa.text(
            "DELETE FROM cashu_transactions "
            "WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY token, type "
            "      ORDER BY created_at ASC, id ASC"
            "    ) AS rn "
            "    FROM cashu_transactions"
            "  ) WHERE rn > 1"
            ")"
        )
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    _dedup_rows(conn)

    existing_indexes = {
        idx["name"] for idx in inspector.get_indexes("cashu_transactions")
    }
    existing_constraints = {
        uc["name"]
        for uc in inspector.get_unique_constraints("cashu_transactions")
    }
    if _CONSTRAINT_NAME not in existing_indexes and _CONSTRAINT_NAME not in existing_constraints:
        op.create_index(
            _CONSTRAINT_NAME,
            "cashu_transactions",
            ["token", "type"],
            unique=True,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_indexes = {
        idx["name"] for idx in inspector.get_indexes("cashu_transactions")
    }
    existing_constraints = {
        uc["name"]
        for uc in inspector.get_unique_constraints("cashu_transactions")
    }
    if _CONSTRAINT_NAME in existing_indexes:
        op.drop_index(_CONSTRAINT_NAME, table_name="cashu_transactions")
    elif _CONSTRAINT_NAME in existing_constraints:
        op.drop_constraint(_CONSTRAINT_NAME, "cashu_transactions")
