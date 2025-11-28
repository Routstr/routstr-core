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

    op.add_column(
        "temporary_credit", sa.Column("created", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "temporary_credit",
        sa.Column("refund_expiration_time", sa.Integer(), nullable=True),
    )
    op.drop_column("temporary_credit", "key_expiry_time")
    op.drop_column("temporary_credit", "total_spent")
    op.drop_column("temporary_credit", "total_requests")


def downgrade() -> None:
    op.add_column(
        "temporary_credit",
        sa.Column(
            "total_requests",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "temporary_credit",
        sa.Column(
            "total_spent",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "temporary_credit", sa.Column("key_expiry_time", sa.Integer(), nullable=True)
    )
    op.drop_column("temporary_credit", "refund_expiration_time")
    op.drop_column("temporary_credit", "created")

    # Revert table rename
    op.rename_table("temporary_credit", "api_keys")
