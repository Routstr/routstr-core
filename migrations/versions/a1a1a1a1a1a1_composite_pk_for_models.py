"""change models to composite primary key (id, upstream_provider_id)

Revision ID: a1a1a1a1a1a1
Revises: f7a8b9c0d1e2
Create Date: 2025-10-20 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1a1a1a1a1a1"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "models" in inspector.get_table_names():
        op.drop_table("models")

    op.create_table(
        "models",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("upstream_provider_id", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", "upstream_provider_id"),
        sa.ForeignKeyConstraint(
            ["upstream_provider_id"], ["upstream_providers.id"], ondelete="CASCADE"
        ),
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
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("upstream_provider_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["upstream_provider_id"], ["upstream_providers.id"]),
    )
