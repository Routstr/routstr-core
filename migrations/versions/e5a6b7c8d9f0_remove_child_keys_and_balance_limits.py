"""Remove child keys and balance limits.

Removes the child-key feature (parent_key_hash) and the balance-limit
machinery (balance_limit, balance_limit_reset, balance_limit_reset_date)
from api_keys, plus the balance_limit/balance_limit_reset pass-through on
lightning_invoices.

Data preservation: before dropping the columns, every child key is
converted into a standalone key by clearing parent_key_hash. Child keys
never hold their own balance (they always spent from their parent), so no
funds are lost: the parent keeps its full balance, and the former child
rows are preserved with their total_spent/total_requests history intact.
"""

import sqlalchemy as sa
from alembic import op

revision = "e5a6b7c8d9f0"
down_revision = "b4f7a1c9d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Convert child keys into standalone keys before dropping the link.
    # Their balance is always 0 (they spent from the parent), so this
    # cannot strand any funds.
    op.execute("UPDATE api_keys SET parent_key_hash = NULL")

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_index("ix_api_keys_parent_key_hash")
        batch_op.drop_column("parent_key_hash")
        batch_op.drop_column("balance_limit")
        batch_op.drop_column("balance_limit_reset")
        batch_op.drop_column("balance_limit_reset_date")

    with op.batch_alter_table("lightning_invoices") as batch_op:
        batch_op.drop_column("balance_limit")
        batch_op.drop_column("balance_limit_reset")


def downgrade() -> None:
    with op.batch_alter_table("lightning_invoices") as batch_op:
        batch_op.add_column(sa.Column("balance_limit", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("balance_limit_reset", sa.String(), nullable=True)
        )

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(
            sa.Column("balance_limit_reset_date", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "balance_limit_reset",
                sa.String(),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("balance_limit", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "parent_key_hash",
                sa.String(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_api_keys_parent_key_hash",
            "api_keys",
            ["parent_key_hash"],
            ["hashed_key"],
        )
        batch_op.create_index(
            "ix_api_keys_parent_key_hash", ["parent_key_hash"], unique=False
        )
