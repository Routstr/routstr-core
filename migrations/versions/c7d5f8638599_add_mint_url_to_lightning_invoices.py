"""add mint url to lightning invoices

Revision ID: c7d5f8638599
Revises: 9c4d8e2f1a6b
Create Date: 2026-07-26 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c7d5f8638599"
down_revision = "9c4d8e2f1a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lightning_invoices", sa.Column("mint_url", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("lightning_invoices", "mint_url")
