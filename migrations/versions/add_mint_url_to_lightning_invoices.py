"""add mint_url to lightning_invoices

Revision ID: add_mint_url_li
Revises: c6d7e8f9a0b1
Create Date: 2026-07-10 02:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "add_mint_url_li"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lightning_invoices", sa.Column("mint_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("lightning_invoices", "mint_url")
