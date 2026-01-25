"""make upstream provider base_url + api_key unique

Revision ID: c2d3e4f5a6b7
Revises: a86e5348850b
Create Date: 2026-01-25 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "a86e5348850b"
branch_labels = None
depends_on = None


def _recreate_table_sqlite(add_base_url_unique: bool) -> None:
    conn = op.get_bind()
    existing_tables = {
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "upstream_providers_old" in existing_tables:
        if "upstream_providers" in existing_tables:
            op.drop_table("upstream_providers_old")
        else:
            op.execute(
                "ALTER TABLE upstream_providers_old RENAME TO upstream_providers"
            )
            existing_tables.add("upstream_providers")
    if "upstream_providers" not in existing_tables:
        return

    constraints = [
        sa.UniqueConstraint(
            "base_url",
            "api_key",
            name="uq_upstream_providers_base_url_api_key",
        )
    ]
    if add_base_url_unique:
        constraints.append(
            sa.UniqueConstraint("base_url", name="uq_upstream_providers_base_url")
        )

    op.execute("ALTER TABLE upstream_providers RENAME TO upstream_providers_old")
    op.create_table(
        "upstream_providers",
        sa.Column(
            "id", sa.Integer(), primary_key=True, nullable=False, autoincrement=True
        ),
        sa.Column("provider_type", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=False),
        sa.Column("api_key", sa.String(), nullable=False),
        sa.Column("api_version", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("provider_fee", sa.Float(), nullable=False, server_default="1.01"),
        *constraints,
    )
    op.execute(
        "INSERT INTO upstream_providers (id, provider_type, base_url, api_key, api_version, enabled, provider_fee) "
        "SELECT id, provider_type, base_url, api_key, api_version, enabled, provider_fee "
        "FROM upstream_providers_old"
    )
    op.drop_table("upstream_providers_old")


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        _recreate_table_sqlite(add_base_url_unique=False)
        return

    inspector = sa.inspect(conn)
    for constraint in inspector.get_unique_constraints("upstream_providers"):
        if constraint.get("column_names") == ["base_url"] and constraint.get("name"):
            op.drop_constraint(
                constraint["name"],
                "upstream_providers",
                type_="unique",
            )
    index_names = {idx["name"] for idx in inspector.get_indexes("upstream_providers")}
    if "ix_upstream_providers_base_url" in index_names:
        op.drop_index("ix_upstream_providers_base_url", table_name="upstream_providers")
    op.create_unique_constraint(
        "uq_upstream_providers_base_url_api_key",
        "upstream_providers",
        ["base_url", "api_key"],
    )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        _recreate_table_sqlite(add_base_url_unique=True)
        return

    op.drop_constraint(
        "uq_upstream_providers_base_url_api_key",
        "upstream_providers",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_upstream_providers_base_url",
        "upstream_providers",
        ["base_url"],
    )
    op.create_index(
        "ix_upstream_providers_base_url",
        "upstream_providers",
        ["base_url"],
        unique=True,
    )
