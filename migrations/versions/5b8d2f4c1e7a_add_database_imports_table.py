"""add database imports table

Revision ID: 5b8d2f4c1e7a
Revises: 20cbae78521c
Create Date: 2026-10-25 12:00:00.000000

A node started with SQLITE_IMPORT_PATH copies that SQLite database into an empty
PostgreSQL database once. The marker row records the copy so a restart with the
same setting does not import again.
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "5b8d2f4c1e7a"
down_revision = "20cbae78521c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("database_imports"):
        return
    op.create_table(
        "database_imports",
        sa.Column(
            "source_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column("source_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_revision", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("imported_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("source_fingerprint"),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("database_imports"):
        op.drop_table("database_imports")
