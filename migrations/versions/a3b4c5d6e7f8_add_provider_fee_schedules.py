"""add provider_fee_schedules to upstream_providers

Revision ID: a3b4c5d6e7f8
Revises: 614c0a740e68
Create Date: 2026-04-12 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a3b4c5d6e7f8"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("upstream_providers")]

    if "provider_fee_schedules" not in columns:
        op.add_column(
            "upstream_providers",
            sa.Column("provider_fee_schedules", sa.Text(), nullable=True),
        )

    if "provider_fee_default" not in columns:
        op.add_column(
            "upstream_providers",
            sa.Column("provider_fee_default", sa.Float(), nullable=True),
        )
        # Initialize default fee from current fee
        op.execute("UPDATE upstream_providers SET provider_fee_default = provider_fee")


def downgrade() -> None:
    op.drop_column("upstream_providers", "provider_fee_schedules")
    op.drop_column("upstream_providers", "provider_fee_default")
