"""add upstream_provider and enabled to models

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2025-10-13 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("models")
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
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("upstream_provider_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["upstream_provider_id"], ["upstream_providers.id"]),
    )


def downgrade() -> None:
    op.drop_table("models")
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
