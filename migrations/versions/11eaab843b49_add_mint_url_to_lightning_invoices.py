"""add mint url to lightning invoices

Revision ID: 11eaab843b49
Revises: d7e8f9a0b1c2
Create Date: 2026-07-22 22:25:45.278261
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "11eaab843b49"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lightning_invoices", sa.Column("mint_url", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("lightning_invoices", "mint_url")
