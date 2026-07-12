"""Add unique token/type index to Cashu transactions.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
"""

from __future__ import annotations

from itertools import groupby

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection, RowMapping

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_cashu_transactions_token_type"
_NULLABLE_METADATA = ("request_id", "mint_url", "api_key_hashed_key")


def _merge_duplicate_transactions(connection: Connection) -> None:
    transactions = sa.Table(
        "cashu_transactions",
        sa.MetaData(),
        autoload_with=connection,
    )
    duplicate_keys = (
        sa.select(transactions.c.token, transactions.c.type)
        .group_by(transactions.c.token, transactions.c.type)
        .having(sa.func.count() > 1)
        .subquery()
    )
    rows = (
        connection.execute(
            sa.select(transactions)
            .join(
                duplicate_keys,
                sa.and_(
                    transactions.c.token == duplicate_keys.c.token,
                    transactions.c.type == duplicate_keys.c.type,
                ),
            )
            .order_by(
                transactions.c.token,
                transactions.c.type,
                transactions.c.created_at,
                transactions.c.id,
            )
        )
        .mappings()
        .all()
    )

    def transaction_key(row: RowMapping) -> tuple[str, str]:
        return row["token"], row["type"]

    for _, grouped_rows in groupby(rows, key=transaction_key):
        duplicates = list(grouped_rows)
        if len(duplicates) < 2:
            continue

        keeper = duplicates[0]
        updates: dict[str, object] = {
            "collected": any(row["collected"] for row in duplicates),
            "swept": any(row["swept"] for row in duplicates),
        }
        for column in _NULLABLE_METADATA:
            if not keeper[column]:
                updates[column] = next(
                    (row[column] for row in duplicates if row[column]),
                    keeper[column],
                )
        if not keeper["source"]:
            updates["source"] = next(
                (row["source"] for row in duplicates if row["source"]),
                keeper["source"],
            )

        connection.execute(
            transactions.update()
            .where(transactions.c.id == keeper["id"])
            .values(**updates)
        )
        connection.execute(
            transactions.delete().where(
                transactions.c.id.in_(row["id"] for row in duplicates[1:])
            )
        )


def upgrade() -> None:
    connection = op.get_bind()
    _merge_duplicate_transactions(connection)
    op.create_index(
        _INDEX_NAME,
        "cashu_transactions",
        ["token", "type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="cashu_transactions")
