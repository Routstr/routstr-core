"""never reuse upstream provider ids

Revision ID: f2a7c9d4e8b1
Revises: ecfa0d6e2a36
Create Date: 2026-08-05 00:40:19.000000
"""

from __future__ import annotations

from alembic import op

revision = "f2a7c9d4e8b1"
down_revision = "ecfa0d6e2a36"
branch_labels = None
depends_on = None


def _recreate_sqlite_table(*, sqlite_autoincrement: bool) -> None:
    with op.batch_alter_table(
        "upstream_providers",
        recreate="always",
        table_kwargs={"sqlite_autoincrement": sqlite_autoincrement},
    ):
        pass


def upgrade() -> None:
    # A bare SQLite INTEGER PRIMARY KEY can reuse the highest deleted row ID.
    # AUTOINCREMENT records a durable high-water mark in sqlite_sequence.
    if op.get_bind().dialect.name == "sqlite":
        _recreate_sqlite_table(sqlite_autoincrement=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _recreate_sqlite_table(sqlite_autoincrement=False)
