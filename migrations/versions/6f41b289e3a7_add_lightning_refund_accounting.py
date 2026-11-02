"""Persist Lightning refund inputs and actual payout accounting.

Revision ID: 6f41b289e3a7
Revises: 424bb59871d4
"""

import sqlalchemy as sa
from alembic import op

revision = "6f41b289e3a7"
down_revision = "424bb59871d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("sent_msats", "input_msats", "spent_msats"):
        op.add_column("refunds", sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("refunds") as batch:
        for name in ("spent_msats", "input_msats", "sent_msats"):
            batch.drop_column(name)
