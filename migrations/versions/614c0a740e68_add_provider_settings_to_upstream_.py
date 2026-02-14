"""add provider_settings to upstream_providers

Revision ID: 614c0a740e68
Revises: 06f81c0fc88d
Create Date: 2026-02-13 22:36:53.608737
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "614c0a740e68"
down_revision = "06f81c0fc88d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if column exists before adding it
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("upstream_providers")]

    if "provider_settings" not in columns:
        op.add_column(
            "upstream_providers",
            sa.Column("provider_settings", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("upstream_providers", "provider_settings")
