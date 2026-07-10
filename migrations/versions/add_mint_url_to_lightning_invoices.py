"""add mint_url to lightning_invoices

Revision ID: add_mint_url_li
Revises: d7e8f9a0b1c2
Create Date: 2026-07-10 23:07:19.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "add_mint_url_li"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lightning_invoices", sa.Column("mint_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("lightning_invoices", "mint_url")
