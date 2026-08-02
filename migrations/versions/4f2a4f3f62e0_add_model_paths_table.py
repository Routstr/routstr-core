"""add model paths table

Revision ID: 4f2a4f3f62e0
Revises: bf76270b66c4
Create Date: 2026-08-02 23:28:24.760061
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "4f2a4f3f62e0"
down_revision = "bf76270b66c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_paths",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("provider_slug", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("provider_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("endpoint_tag", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("endpoint_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("upstream_provider_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["upstream_provider_id"], ["upstream_providers.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "model_id",
            "path",
            "upstream_provider_id",
            name="uq_model_paths_model_path_provider",
        ),
    )
    op.create_index(
        op.f("ix_model_paths_upstream_provider_id"),
        "model_paths",
        ["upstream_provider_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_model_paths_upstream_provider_id"), table_name="model_paths")
    op.drop_table("model_paths")
