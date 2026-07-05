"""add model_paths table

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-07-05 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "model_paths" in inspector.get_table_names():
        return

    op.create_table(
        "model_paths",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("upstream_provider_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["upstream_provider_id"],
            ["upstream_providers.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "model_id",
            "path",
            "upstream_provider_id",
            name="uq_model_paths_model_path_provider",
        ),
    )
    op.create_index(
        "ix_model_paths_model_id",
        "model_paths",
        ["model_id"],
    )
    op.create_index(
        "ix_model_paths_upstream_provider_id",
        "model_paths",
        ["upstream_provider_id"],
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "model_paths" not in inspector.get_table_names():
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("model_paths")}
    if "ix_model_paths_upstream_provider_id" in existing_indexes:
        op.drop_index("ix_model_paths_upstream_provider_id", table_name="model_paths")
    if "ix_model_paths_model_id" in existing_indexes:
        op.drop_index("ix_model_paths_model_id", table_name="model_paths")
    op.drop_table("model_paths")
