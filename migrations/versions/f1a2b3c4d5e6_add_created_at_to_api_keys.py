"""add created_at to api_keys

Revision ID: f1a2b3c4d5e6
Revises: cli_tokens_001
Create Date: 2026-06-01 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f1a2b3c4d5e6"
down_revision = "cli_tokens_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable on purpose: existing keys keep NULL (unknown creation time) and
    # sort last; new keys get populated by the model's default_factory.
    op.add_column("api_keys", sa.Column("created_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "created_at")
