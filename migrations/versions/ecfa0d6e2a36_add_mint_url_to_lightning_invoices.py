"""add mint url to lightning invoices

Revision ID: ecfa0d6e2a36
Revises: 64ed5594df1f
Create Date: 2026-08-02 23:53:00.037456
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "ecfa0d6e2a36"
down_revision = "64ed5594df1f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lightning_invoices", sa.Column("mint_url", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("lightning_invoices", "mint_url")
