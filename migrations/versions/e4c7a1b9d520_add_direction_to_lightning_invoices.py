"""Add direction to lightning_invoices

Revision ID: e4c7a1b9d520
Revises: 3a0fbd387f10
Create Date: 2026-09-20 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "e4c7a1b9d520"
down_revision = "3a0fbd387f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lightning_invoices",
        sa.Column("direction", sa.String(), nullable=False, server_default="in"),
    )


def downgrade() -> None:
    op.drop_column("lightning_invoices", "direction")
