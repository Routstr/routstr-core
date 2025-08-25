"""init

Revision ID: f6ce1348e266
Revises:
Create Date: 2025-08-09 13:28:38.537652
"""

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql import sqltypes

# revision identifiers, used by Alembic.
revision = "f6ce1348e266"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "api_keys" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "api_keys",
            sa.Column("hashed_key", sqltypes.AutoString(), nullable=False),
            sa.Column("balance", sa.Integer(), nullable=False),
            sa.Column("refund_address", sqltypes.AutoString(), nullable=True),
            sa.Column("key_expiry_time", sa.Integer(), nullable=True),
            sa.Column("total_spent", sa.Integer(), nullable=False),
            sa.Column("total_requests", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("hashed_key"),
        )


def downgrade() -> None:
    # Only drop the table if it exists
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if "api_keys" in tables:
        op.drop_table("api_keys")
