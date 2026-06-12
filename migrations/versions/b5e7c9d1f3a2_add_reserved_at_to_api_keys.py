"""add reserved_at to api_keys

Revision ID: b5e7c9d1f3a2
Revises: a2b3c4d5e6f7
Create Date: 2026-06-12 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b5e7c9d1f3a2"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # existing keys keep NULL
    # New reservations populate it via pay_for_request.
    op.add_column("api_keys", sa.Column("reserved_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "reserved_at")
