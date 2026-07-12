"""add mint url to lightning invoices

Revision ID: 21c84cd5ad83
Revises: c6d7e8f9a0b1
Create Date: 2026-07-12 15:04:01.675455
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "21c84cd5ad83"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lightning_invoices", sa.Column("mint_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("lightning_invoices", "mint_url")
