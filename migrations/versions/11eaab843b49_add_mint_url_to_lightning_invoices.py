"""add mint url to lightning invoices

Revision ID: 11eaab843b49
Revises: d7e8f9a0b1c2
Create Date: 2026-07-22 22:25:45.278261
"""

import sqlalchemy as sa
from alembic import op

# This revision was deployed from the pre-merge branch. Keep it in the graph so
# those databases can migrate forward instead of being stamped past migrations.
revision = "11eaab843b49"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("lightning_invoices")
    }
    if "mint_url" not in columns:
        op.add_column(
            "lightning_invoices",
            sa.Column("mint_url", sa.String(), nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("lightning_invoices")
    }
    if "mint_url" in columns:
        op.drop_column("lightning_invoices", "mint_url")
