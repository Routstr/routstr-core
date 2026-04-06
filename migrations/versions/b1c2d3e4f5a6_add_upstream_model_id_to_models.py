"""add forwarded_model_id to models

Revision ID: b1c2d3e4f5a6
Revises: a776ca70e5fe
Create Date: 2026-04-05 00:00:00.000000
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "b1c2d3e4f5a6"
down_revision = "a776ca70e5fe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "models",
        sa.Column(
            "forwarded_model_id",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
    )
    # Backfill: set forwarded_model_id = id for all existing rows
    op.execute("UPDATE models SET forwarded_model_id = id WHERE forwarded_model_id IS NULL")


def downgrade() -> None:
    op.drop_column("models", "forwarded_model_id")
