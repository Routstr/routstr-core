"""add provider_fee_schedules and provider_fee_default to upstream_providers

Revision ID: 6d2fa295fa43
Revises: cli_tokens_001
Create Date: 2026-04-28 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "6d2fa295fa43"
down_revision = "cli_tokens_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("upstream_providers")}

    if "provider_fee_default" not in columns:
        op.add_column(
            "upstream_providers",
            sa.Column(
                "provider_fee_default",
                sa.Float(),
                nullable=False,
                server_default="1.01",
            ),
        )
        # Preserve any custom per-provider fees by copying from provider_fee.
        op.execute(
            "UPDATE upstream_providers "
            "SET provider_fee_default = provider_fee "
            "WHERE provider_fee IS NOT NULL"
        )

    if "provider_fee_schedules" not in columns:
        op.add_column(
            "upstream_providers",
            sa.Column("provider_fee_schedules", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("upstream_providers")}

    if "provider_fee_schedules" in columns:
        op.drop_column("upstream_providers", "provider_fee_schedules")
    if "provider_fee_default" in columns:
        op.drop_column("upstream_providers", "provider_fee_default")
