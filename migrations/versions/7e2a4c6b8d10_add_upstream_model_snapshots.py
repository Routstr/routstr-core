"""add upstream model snapshots

Revision ID: 7e2a4c6b8d10
Revises: 5c7d9e1f2a3b
Create Date: 2026-10-11 18:00:00.000000

Every worker fetched each provider's model catalog on its own and kept it only in
process memory. A fetch that failed or was rate limited left that worker with no
models, so behind a load balancer most requests answered "model not found" while
a sibling served hundreds. The last good catalog now lives in the database where
every worker can fall back to it.
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "7e2a4c6b8d10"
down_revision = "5c7d9e1f2a3b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "upstream_model_snapshots",
        sa.Column(
            "upstream_provider_id", sa.Integer(), autoincrement=False, nullable=False
        ),
        sa.Column("config_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("models_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("fetched_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("upstream_provider_id"),
    )


def downgrade() -> None:
    op.drop_table("upstream_model_snapshots")
