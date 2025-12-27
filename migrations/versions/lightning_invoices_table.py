"""Add lightning_invoices table

Revision ID: lightning_invoices
Revises: a1a1a1a1a1a1
Create Date: 2025-12-10 21:00:00.000000
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "lightning_invoices"
down_revision = "a1a1a1a1a1a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lightning_invoices",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("bolt11", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("amount_sats", sa.Integer(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("payment_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("api_key_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("purpose", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("paid_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bolt11"),
        sa.UniqueConstraint("payment_hash"),
    )


def downgrade() -> None:
    op.drop_table("lightning_invoices")
