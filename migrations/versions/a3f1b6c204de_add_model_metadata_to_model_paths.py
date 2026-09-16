"""add model_metadata to model_paths

Revision ID: a3f1b6c204de
Revises: e5a6b7c8d9f0
Create Date: 2026-09-15 21:50:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3f1b6c204de"
down_revision = "e5a6b7c8d9f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_paths",
        sa.Column("model_metadata", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("model_paths", "model_metadata")
