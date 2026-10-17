"""add admin sessions table

Revision ID: f38b16f1a3dd
Revises: 74d9ee257408
Create Date: 2026-10-18 10:01:00.000000

Dashboard logins used to live in a per-process dict, so with several uvicorn
workers or replicas an admin call only succeeded on the worker that issued the
token. The database is the one store every worker shares.
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "f38b16f1a3dd"
down_revision = "74d9ee257408"
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
