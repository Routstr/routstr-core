"""create models table

Revision ID: c0ffee123456
Revises: a1b2c3d4e5f6
Create Date: 2025-09-10 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c0ffee123456"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "models",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("context_length", sa.Integer(), nullable=False),
        sa.Column("architecture", sa.Text(), nullable=False),
        sa.Column("pricing", sa.Text(), nullable=False),
        sa.Column("sats_pricing", sa.Text(), nullable=True),
        sa.Column("per_request_limits", sa.Text(), nullable=True),
        sa.Column("top_provider", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("models")
