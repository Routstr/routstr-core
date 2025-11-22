"""create upstream_providers table

Revision ID: d1e2f3a4b5c6
Revises: c0ffee123456
Create Date: 2025-10-09 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c0ffee123456"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "upstream_providers" not in inspector.get_table_names():
        op.create_table(
            "upstream_providers",
            sa.Column(
                "id", sa.Integer(), primary_key=True, nullable=False, autoincrement=True
            ),
            sa.Column("provider_type", sa.String(), nullable=False),
            sa.Column("base_url", sa.String(), nullable=False, unique=True),
            sa.Column("api_key", sa.String(), nullable=False),
            sa.Column("api_version", sa.String(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, default=True),
        )
        op.create_index(
            "ix_upstream_providers_base_url",
            "upstream_providers",
            ["base_url"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index("ix_upstream_providers_base_url", "upstream_providers")
    op.drop_table("upstream_providers")
