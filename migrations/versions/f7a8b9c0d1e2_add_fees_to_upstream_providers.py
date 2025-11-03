"""add provider_fee to upstream_providers

Revision ID: f7a8b9c0d1e2
Revises: e1f2a3b4c5d6
Create Date: 2025-10-13 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "upstream_providers",
        sa.Column("provider_fee", sa.Float(), nullable=False, server_default="1.01"),
    )


def downgrade() -> None:
    op.drop_column("upstream_providers", "provider_fee")
