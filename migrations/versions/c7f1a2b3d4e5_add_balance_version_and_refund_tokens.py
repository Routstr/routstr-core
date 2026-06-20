"""add balance_version to api_keys and refund_tokens table (core #412)

Durable, balance-versioned refund idempotency:
  * ``api_keys.balance_version`` is bumped atomically on every credit.
  * ``refund_tokens`` records the refund issued at each (api_key_hash,
    balance_version); an in-flight retry at the same version re-serves the
    stored token, while any later credit (which bumps the version) invalidates
    it.

Revision ID: c7f1a2b3d4e5
Revises: b5e7c9d1f3a2
Create Date: 2026-06-20 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c7f1a2b3d4e5"
down_revision = "b5e7c9d1f3a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows default to 0; the first credit moves them to 1. There are no
    # historical refund tokens to migrate (the old cache was in-memory only).
    op.add_column(
        "api_keys",
        sa.Column(
            "balance_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "refund_tokens",
        sa.Column("api_key_hash", sa.String(), nullable=False),
        sa.Column("balance_version", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(), nullable=False, server_default="sat"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("redeemed_at", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["api_key_hash"], ["api_keys.hashed_key"]
        ),
        sa.PrimaryKeyConstraint("api_key_hash", "balance_version"),
    )


def downgrade() -> None:
    op.drop_table("refund_tokens")
    op.drop_column("api_keys", "balance_version")
