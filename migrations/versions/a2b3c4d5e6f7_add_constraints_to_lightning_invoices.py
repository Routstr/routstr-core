"""Add balance_limit, balance_limit_reset, validity_date to lightning_invoices

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-03 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lightning_invoices", sa.Column("balance_limit", sa.Integer(), nullable=True))
    op.add_column("lightning_invoices", sa.Column("balance_limit_reset", sa.String(), nullable=True))
    op.add_column("lightning_invoices", sa.Column("validity_date", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("lightning_invoices", "validity_date")
    op.drop_column("lightning_invoices", "balance_limit_reset")
    op.drop_column("lightning_invoices", "balance_limit")
