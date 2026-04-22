"""add provider_fee_schedules to upstream_providers

Revision ID: d9aa7c5fac6b
Revises: d4e5f6a7b8c9
Create Date: 2026-04-23 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d9aa7c5fac6b"
down_revision = "d4e5f6a7b8c9"
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
