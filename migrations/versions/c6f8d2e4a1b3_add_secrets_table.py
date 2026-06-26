"""add secrets table

Revision ID: c6f8d2e4a1b3
Revises: b5e7c9d1f3a2
Create Date: 2026-06-24 00:00:00.000000

Creates the node-level singleton secret store (issue #553). Schema only; moving
any legacy plaintext into the encrypted/hashed columns happens at bootstrap,
where the live ROUTSTR_SECRET_KEY is available.
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "c6f8d2e4a1b3"
down_revision = "b5e7c9d1f3a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "admin_password_hash",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column(
            "encrypted_nsec",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=True,
        ),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("secrets")
