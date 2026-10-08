"""Store upstream-discovered model endpoint capabilities and unit pricing.

Revision ID: 8d71c5a2f903
Revises: 424bb59871d4
"""

import sqlalchemy as sa
from alembic import op

revision = "8d71c5a2f903"
down_revision = "424bb59871d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("models", sa.Column("api_capabilities", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("models", "api_capabilities")
