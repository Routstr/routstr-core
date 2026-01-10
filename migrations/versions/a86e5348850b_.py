"""

Revision ID: a86e5348850b
Revises: b9667ffc5701
Create Date: 2026-01-10 18:57:48.475781
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision = "a86e5348850b"
down_revision = "b9667ffc5701"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use batch_alter_table for SQLite compatibility
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "parent_key_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True
            )
        )
        batch_op.create_index(
            batch_op.f("ix_api_keys_parent_key_hash"), ["parent_key_hash"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_api_keys_parent_key_hash",
            "api_keys",
            ["parent_key_hash"],
            ["hashed_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_constraint("fk_api_keys_parent_key_hash", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_api_keys_parent_key_hash"))
        batch_op.drop_column("parent_key_hash")
