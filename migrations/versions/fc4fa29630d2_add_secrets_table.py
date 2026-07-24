"""add secrets table

Revision ID: fc4fa29630d2
Revises: d7e8f9a0b1c2
Create Date: 2026-07-23 00:00:00.000000

Creates the node-level singleton secret store (issue #553). Schema only; moving
any legacy plaintext into the encrypted/hashed columns happens at bootstrap,
where the live ROUTSTR_SECRET_KEY is available. ``nsec_state`` records the vault's
ownership of the nsec (legacy | encrypted | cleared), so a cleared identity is
never resurrected from a stale legacy ``NSEC`` env var / settings blob on the next
boot.
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "fc4fa29630d2"
down_revision = "d7e8f9a0b1c2"
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
        sa.Column(
            "nsec_state",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default="legacy",
        ),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("secrets")
