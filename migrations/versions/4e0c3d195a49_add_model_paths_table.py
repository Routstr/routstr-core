"""add model paths table

Revision ID: 4e0c3d195a49
Revises: 9c4d8e2f1a6b
Create Date: 2026-07-24 21:14:39.062179
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "4e0c3d195a49"
down_revision = "9c4d8e2f1a6b"
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
        sa.Column("updated_at", sa.Integer(), nullable=False, server_default="0"),
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
    # No standalone index on model_id: the unique constraint's autoindex already
    # leads on model_id.
    op.create_index(
        op.f("ix_model_paths_upstream_provider_id"),
        "model_paths",
        ["upstream_provider_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_model_paths_upstream_provider_id"), table_name="model_paths")
    op.drop_table("model_paths")
