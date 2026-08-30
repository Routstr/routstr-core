"""add model metadata to model paths

Revision ID: e5f6a7b8c9d0
Revises: b4f7a1c9d2e3
Create Date: 2026-08-30 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "b4f7a1c9d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_paths",
        sa.Column(
            "model_metadata",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("model_paths", "model_metadata")
