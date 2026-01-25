"""make upstream provider base_url + api_key unique

Revision ID: c2d3e4f5a6b7
Revises: a86e5348850b
Create Date: 2026-01-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "a86e5348850b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("upstream_providers", recreate="always") as batch_op:
        batch_op.drop_index("ix_upstream_providers_base_url")
        batch_op.alter_column(
            "base_url",
            existing_type=sa.String(),
            nullable=False,
            unique=False,
        )
        batch_op.create_unique_constraint(
            "uq_upstream_providers_base_url_api_key",
            ["base_url", "api_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("upstream_providers", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "uq_upstream_providers_base_url_api_key",
            type_="unique",
        )
        batch_op.alter_column(
            "base_url",
            existing_type=sa.String(),
            nullable=False,
            unique=True,
        )
        batch_op.create_index(
            "ix_upstream_providers_base_url",
            ["base_url"],
            unique=True,
        )
