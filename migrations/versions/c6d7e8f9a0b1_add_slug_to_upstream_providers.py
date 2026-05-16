"""add slug to upstream_providers

Revision ID: c6d7e8f9a0b1
Revises: b5e7c9d1f3a2
Create Date: 2026-06-29 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "b5e7c9d1f3a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("upstream_providers")}

    if "slug" not in columns:
        op.add_column(
            "upstream_providers",
            sa.Column("slug", sa.String(), nullable=True),
        )

    op.execute(
        "UPDATE upstream_providers "
        "SET slug = LOWER(provider_type) || '-' || CAST(id AS TEXT) "
        "WHERE slug IS NULL OR slug = ''"
    )

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("upstream_providers")}
    if "ix_upstream_providers_slug" not in existing_indexes:
        op.create_index(
            "ix_upstream_providers_slug",
            "upstream_providers",
            ["slug"],
            unique=True,
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_indexes = {idx["name"] for idx in inspector.get_indexes("upstream_providers")}
    if "ix_upstream_providers_slug" in existing_indexes:
        op.drop_index("ix_upstream_providers_slug", table_name="upstream_providers")

    columns = {c["name"] for c in inspector.get_columns("upstream_providers")}
    if "slug" in columns:
        op.drop_column("upstream_providers", "slug")
