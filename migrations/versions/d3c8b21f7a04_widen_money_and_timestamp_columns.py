"""widen money and timestamp columns to 64-bit

Revision ID: d3c8b21f7a04
Revises: e4c7a1b9d520
Create Date: 2026-09-29 00:00:00.000000

Balances are millisatoshis and every clock column is a unix timestamp, so both
are 64-bit quantities. SQLite stores all INTEGER values as 64-bit, which is why
plain ``Integer`` columns were safe there — but SQLAlchemy's ``Integer`` maps to
PostgreSQL ``INT4``. On PostgreSQL that caps a key balance at 2_147_483_647
msats (~0.0215 BTC, asyncpg raises "value out of int32 range" past it), lets
lifetime counters like ``total_spent`` and ``total_paid_msats`` overflow, and
puts every unix timestamp on the 2038 cliff — a long-dated ``key_expiry_time``
or invoice ``validity_date`` can already exceed INT4 today.

ALTER TYPE is a no-op on SQLite (its INTEGER is already 64-bit and rewriting the
tables through batch mode would needlessly churn the whole DB), so this only
runs where the width is real.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3c8b21f7a04"
down_revision = "e4c7a1b9d520"
branch_labels = None
depends_on = None


# (table, column, nullable) for every monetary, lifetime-counter and unix
# timestamp column. Surrogate keys, foreign keys and ``models.context_length``
# stay INT4: they are identifiers and bounded magnitudes, not money or clocks.
WIDENED_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("api_keys", "balance", False),
    ("api_keys", "reserved_balance", False),
    ("api_keys", "total_spent", False),
    ("api_keys", "total_requests", False),
    ("api_keys", "reserved_at", True),
    ("api_keys", "key_expiry_time", True),
    ("api_keys", "created_at", True),
    ("api_keys", "validity_date", True),
    ("cashu_transactions", "amount", False),
    ("cashu_transactions", "created_at", False),
    ("cashu_transactions", "sweep_started_at", True),
    ("cli_tokens", "created_at", False),
    ("cli_tokens", "last_used_at", True),
    ("cli_tokens", "expires_at", True),
    ("lightning_invoices", "amount_sats", False),
    ("lightning_invoices", "created_at", False),
    ("lightning_invoices", "expires_at", False),
    ("lightning_invoices", "paid_at", True),
    ("lightning_invoices", "validity_date", True),
    ("model_paths", "updated_at", False),
    ("models", "created", False),
    ("refunds", "amount_msats", False),
    ("refunds", "claimed_at", True),
    ("refunds", "created_at", False),
    ("refunds", "updated_at", False),
    ("reservation_releases", "reserved_msats", False),
    ("reservation_releases", "created_at", False),
    ("routstr_fees", "accumulated_msats", False),
    ("routstr_fees", "total_paid_msats", False),
    ("routstr_fees", "payout_in_progress_msats", False),
    ("routstr_fees", "last_paid_at", True),
    ("routstr_fees", "payout_started_at", True),
    ("secrets", "updated_at", True),
)


def _retype(target: sa.types.TypeEngine, existing: sa.types.TypeEngine) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite INTEGER is already 64-bit; a batch recreate would rewrite every
        # table for a change that does not exist on this backend.
        return
    for table, column, nullable in WIDENED_COLUMNS:
        # Missing schema is an error, not a reason to stamp a partial upgrade.
        op.alter_column(
            table,
            column,
            type_=target,
            existing_type=existing,
            existing_nullable=nullable,
        )


def upgrade() -> None:
    _retype(sa.BigInteger(), sa.Integer())


def downgrade() -> None:
    # Narrowing back to INT4 fails loudly on any row that outgrew it rather than
    # silently truncating a balance, which is the correct outcome here.
    _retype(sa.Integer(), sa.BigInteger())
