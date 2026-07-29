"""add mint url to lightning invoices

Revision ID: bf76270b66c4
Revises: aa50fde387a2
Create Date: 2026-07-30 00:54:30.306876
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "bf76270b66c4"
down_revision = "aa50fde387a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lightning_invoices", sa.Column("mint_url", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("lightning_invoices", "mint_url")
