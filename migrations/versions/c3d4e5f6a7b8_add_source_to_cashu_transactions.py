"""add source to cashu_transactions

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-04-10 00:00:00.000000
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("cashu_transactions")]
    if "source" not in columns:
        op.add_column(
            "cashu_transactions",
            sa.Column(
                "source",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="x-cashu",
            ),
        )


def downgrade() -> None:
    op.drop_column("cashu_transactions", "source")
