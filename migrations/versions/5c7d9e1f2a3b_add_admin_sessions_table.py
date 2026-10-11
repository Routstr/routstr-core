"""add admin sessions table

Revision ID: 5c7d9e1f2a3b
Revises: 3b1965ea9cea
Create Date: 2026-10-10 23:30:00.000000

Dashboard logins used to live in a per-process dict, so with several uvicorn
workers or replicas an admin call only succeeded on the worker that issued the
token. The database is the one store every worker shares.
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "5c7d9e1f2a3b"
down_revision = "3b1965ea9cea"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("token_hash"),
    )


def downgrade() -> None:
    op.drop_table("admin_sessions")
