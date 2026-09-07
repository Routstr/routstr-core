"""add model metadata to model paths

Revision ID: d4597091cd76
Revises: e5a6b7c8d9f0
Create Date: 2026-09-07 22:17:55.426282
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d4597091cd76"
down_revision = "e5a6b7c8d9f0"
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
