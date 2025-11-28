"""the refactor

Revision ID: 5f6ac1e4fa9f
Revises: a1a1a1a1a1a1
Create Date: 2025-11-28 13:31:49.796461
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "5f6ac1e4fa9f"
down_revision = "a1a1a1a1a1a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename the table
    op.rename_table("api_keys", "temporary_credit")

    # Perform column modifications in batch mode for SQLite support
    with op.batch_alter_table("temporary_credit", schema=None) as batch_op:
        batch_op.add_column(sa.Column("created", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("refund_expiration_time", sa.Integer(), nullable=True)
        )
        batch_op.drop_column("key_expiry_time")
        batch_op.drop_column("total_spent")
        batch_op.drop_column("total_requests")


def downgrade() -> None:
    # Revert column modifications
    with op.batch_alter_table("temporary_credit", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "total_requests",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "total_spent",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(sa.Column("key_expiry_time", sa.Integer(), nullable=True))
        batch_op.drop_column("refund_expiration_time")
        batch_op.drop_column("created")

    # Revert table rename
    op.rename_table("temporary_credit", "api_keys")
