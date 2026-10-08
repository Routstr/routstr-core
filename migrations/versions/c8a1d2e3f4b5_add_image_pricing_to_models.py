"""add image_pricing to models

Revision ID: c8a1d2e3f4b5
Revises: 424bb59871d4
Create Date: 2026-09-24 00:00:00.000000
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "c8a1d2e3f4b5"
down_revision = "424bb59871d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "models",
        sa.Column(
            "image_pricing",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("models", "image_pricing")
