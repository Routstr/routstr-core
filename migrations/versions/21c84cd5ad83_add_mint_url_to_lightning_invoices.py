"""add mint url to lightning invoices

Revision ID: 21c84cd5ad83
Revises: c6d7e8f9a0b1
Create Date: 2026-07-12 15:04:01.675455
"""

import sqlalchemy as sa
from alembic import op

# This revision was deployed before the migration was recreated. Retain it so
# databases stamped with it remain connected to the migration graph.
revision = "21c84cd5ad83"
down_revision = "c6d7e8f9a0b1"
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
