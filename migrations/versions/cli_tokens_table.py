"""add cli_tokens table

Revision ID: cli_tokens_001
Revises: e8f9a0b1c2d3
Create Date: 2026-04-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "cli_tokens_001"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cli_tokens",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("token", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.Integer(), nullable=True),
    )
    op.create_index("ix_cli_tokens_token", "cli_tokens", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_cli_tokens_token", table_name="cli_tokens")
    op.drop_table("cli_tokens")
