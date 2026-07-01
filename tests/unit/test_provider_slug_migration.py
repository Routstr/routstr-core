from __future__ import annotations

import importlib

import sqlalchemy as sa

migration = importlib.import_module(
    "migrations.versions.c6d7e8f9a0b1_add_slug_to_upstream_providers"
)


def test_slug_migration_backfill_uses_api_safe_deterministic_slugs() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE upstream_providers ("
                "id INTEGER PRIMARY KEY, "
                "provider_type VARCHAR NOT NULL, "
                "slug VARCHAR NULL"
                ")"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO upstream_providers (id, provider_type, slug) VALUES "
                "(1, 'OpenAI Compatible', NULL), "
                "(2, 'OpenAI Compatible', ''), "
                "(3, '123', NULL), "
                "(4, 'x', NULL), "
                "(5, 'anthropic', 'anthropic')"
            )
        )

        migration._backfill_provider_slugs(conn)

        rows = conn.execute(
            sa.text("SELECT id, slug FROM upstream_providers ORDER BY id")
        ).all()

    assert rows == [
        (1, "openai-compatible"),
        (2, "openai-compatible-2"),
        (3, "provider-123"),
        (4, "x-provider"),
        (5, "anthropic"),
    ]
