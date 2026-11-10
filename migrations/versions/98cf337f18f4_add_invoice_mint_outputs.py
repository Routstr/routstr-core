"""add the outputs reserved for a Lightning invoice quote

Revision ID: 98cf337f18f4
Revises: e1b7c3d9a204
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "98cf337f18f4"
down_revision = "e1b7c3d9a204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lightning_invoices", sa.Column("mint_keyset_id", sa.String(), nullable=True)
    )
    op.add_column(
        "lightning_invoices", sa.Column("mint_counter", sa.Integer(), nullable=True)
    )
    op.add_column(
        "lightning_invoices", sa.Column("mint_outputs", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("lightning_invoices") as batch_op:
        batch_op.drop_column("mint_outputs")
        batch_op.drop_column("mint_counter")
        batch_op.drop_column("mint_keyset_id")
