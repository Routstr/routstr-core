"""encrypt upstream provider api keys

Revision ID: a3f1c7b25e94
Revises: b4f7a1c9d2e3
Create Date: 2026-08-24 00:00:00.000000

Provider credentials were a plaintext ``api_key`` column, so a copy of the
database or a backup handed over every upstream key. They move to
``encrypted_api_key`` (Fernet, ``routstr.core.vault``) plus a keyed
``api_key_fingerprint`` that preserves the ``(base_url, api_key)`` uniqueness
and the duplicate check without decrypting anything.

Unlike the ``secrets`` table this cannot be deferred to bootstrap: the plaintext
column has to disappear in the same step that the ciphertext appears, or the
secret stays readable. Alembic runs the whole revision in one transaction, so a
crash rolls back to plaintext — recoverable — rather than to a table with keys
in neither column.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from routstr.core.vault import decrypt, encrypt, fingerprint, is_encrypted

revision = "a3f1c7b25e94"
down_revision = "b4f7a1c9d2e3"
branch_labels = None
depends_on = None

_COLUMNS = (
    "id, slug, provider_type, base_url, api_version, enabled, provider_fee, "
    "provider_settings"
)


def _recreate(key_columns: list[sa.Column], unique_with_base_url: str) -> None:
    """Rebuild ``upstream_providers`` around a different set of key columns.

    SQLite cannot drop a column a unique constraint names, so both directions
    rebuild the table rather than alter it. Rows are reinserted by the caller,
    which already holds the transformed keys.
    """
    op.drop_table("upstream_providers")
    op.create_table(
        "upstream_providers",
        sa.Column(
            "id", sa.Integer(), primary_key=True, nullable=False, autoincrement=True
        ),
        sa.Column("slug", sa.String(), nullable=True, unique=True, index=True),
        sa.Column("provider_type", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=False),
        *key_columns,
        sa.Column("api_version", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("provider_fee", sa.Float(), nullable=False, server_default="1.01"),
        sa.Column("provider_settings", sa.String(), nullable=True),
        sa.UniqueConstraint(
            "base_url",
            unique_with_base_url,
            name="uq_upstream_providers_base_url_api_key",
        ),
        sqlite_autoincrement=True,
    )


def _restore_legacy_upstream_api_key(conn: sa.Connection) -> None:
    """Put the node-scoped upstream key back in the settings blob.

    Bootstrap moved it out of the blob and into ``secrets``; the previous build
    only knows how to read the blob, so a downgrade that just dropped the column
    would silently leave the node with no upstream credential.
    """
    row = conn.execute(
        sa.text("SELECT encrypted_upstream_api_key FROM secrets WHERE id = 1")
    ).fetchone()
    if row is None or not row[0]:
        return
    blob = conn.execute(sa.text("SELECT data FROM settings WHERE id = 1")).fetchone()
    if blob is None:
        return
    data = json.loads(blob[0])
    data["upstream_api_key"] = decrypt(row[0])
    conn.execute(
        sa.text("UPDATE settings SET data = :data WHERE id = 1"),
        {"data": json.dumps(data)},
    )


def upgrade() -> None:
    # Schema only for the node-scoped legacy key; moving the plaintext out of the
    # settings blob happens at bootstrap, where the live master key is available.
    op.add_column(
        "secrets", sa.Column("encrypted_upstream_api_key", sa.String(), nullable=True)
    )

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(f"SELECT {_COLUMNS}, api_key FROM upstream_providers")
    ).fetchall()

    # Transform before any DDL: if a key cannot be encrypted the revision aborts
    # with the plaintext table still intact.
    migrated = [
        (
            *row[:-1],
            encrypt(row[-1]) if row[-1] else "",
            fingerprint(row[-1]) if row[-1] else "",
        )
        for row in rows
    ]

    _recreate(
        [
            sa.Column(
                "encrypted_api_key", sa.String(), nullable=False, server_default=""
            ),
            sa.Column(
                "api_key_fingerprint",
                sa.String(),
                nullable=False,
                server_default="",
                index=True,
            ),
        ],
        unique_with_base_url="api_key_fingerprint",
    )
    if migrated:
        conn.execute(
            sa.text(
                f"INSERT INTO upstream_providers ({_COLUMNS}, encrypted_api_key, "
                "api_key_fingerprint) VALUES (:id, :slug, :provider_type, :base_url, "
                ":api_version, :enabled, :provider_fee, :provider_settings, "
                ":encrypted_api_key, :api_key_fingerprint)"
            ),
            [
                dict(
                    zip(
                        (
                            "id",
                            "slug",
                            "provider_type",
                            "base_url",
                            "api_version",
                            "enabled",
                            "provider_fee",
                            "provider_settings",
                            "encrypted_api_key",
                            "api_key_fingerprint",
                        ),
                        row,
                    )
                )
                for row in migrated
            ],
        )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(f"SELECT {_COLUMNS}, encrypted_api_key FROM upstream_providers")
    ).fetchall()

    # Fails closed on a missing or wrong ROUTSTR_SECRET_KEY: aborting with the
    # ciphertext intact is recoverable, writing undecryptable bytes into a column
    # the previous build reads as a bearer token is not.
    restored = [
        (*row[:-1], decrypt(row[-1]) if is_encrypted(row[-1]) else row[-1])
        for row in rows
    ]
    _restore_legacy_upstream_api_key(conn)
    op.drop_column("secrets", "encrypted_upstream_api_key")

    _recreate(
        [sa.Column("api_key", sa.String(), nullable=False, server_default="")],
        unique_with_base_url="api_key",
    )
    if restored:
        conn.execute(
            sa.text(
                f"INSERT INTO upstream_providers ({_COLUMNS}, api_key) "
                "VALUES (:id, :slug, :provider_type, :base_url, :api_version, "
                ":enabled, :provider_fee, :provider_settings, :api_key)"
            ),
            [
                dict(
                    zip(
                        (
                            "id",
                            "slug",
                            "provider_type",
                            "base_url",
                            "api_version",
                            "enabled",
                            "provider_fee",
                            "provider_settings",
                            "api_key",
                        ),
                        row,
                    )
                )
                for row in restored
            ],
        )
