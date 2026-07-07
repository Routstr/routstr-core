"""add slug to upstream_providers

Revision ID: c6d7e8f9a0b1
Revises: b5e7c9d1f3a2
Create Date: 2026-06-29 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from routstr.core.provider_slugs import provider_slug_base, provider_slug_candidate

revision = "c6d7e8f9a0b1"
down_revision = "b5e7c9d1f3a2"
branch_labels = None
depends_on = None


def _allocate_backfill_slug(provider_type: str, reserved_slugs: set[str]) -> str:
    base = provider_slug_base(provider_type)
    suffix_number = 1
    while True:
        candidate = provider_slug_candidate(base, suffix_number)
        if candidate not in reserved_slugs:
            reserved_slugs.add(candidate)
            return candidate
        suffix_number += 1


def _backfill_provider_slugs(conn: sa.Connection) -> None:
    existing_rows = conn.execute(
        sa.text(
            "SELECT slug FROM upstream_providers "
            "WHERE slug IS NOT NULL AND slug != ''"
        )
    )
    reserved_slugs = {str(row.slug).lower() for row in existing_rows}

    rows_to_backfill = conn.execute(
        sa.text(
            "SELECT id, provider_type FROM upstream_providers "
            "WHERE slug IS NULL OR slug = '' "
            "ORDER BY id"
        )
    )
    for row in rows_to_backfill:
        slug = _allocate_backfill_slug(str(row.provider_type), reserved_slugs)
        conn.execute(
            sa.text("UPDATE upstream_providers SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": row.id},
        )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("upstream_providers")}

    if "slug" not in columns:
        op.add_column(
            "upstream_providers",
            sa.Column("slug", sa.String(), nullable=True),
        )

    _backfill_provider_slugs(conn)

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
