"""Tests for upstream provider API keys being encrypted at rest.

Provider keys used to sit in a plaintext ``upstream_providers.api_key`` column,
so anyone holding a copy of the SQLite file or a backup held every upstream
credential. They now live in ``encrypted_api_key`` (Fernet, via
``routstr.core.vault``) alongside a keyed ``api_key_fingerprint`` that keeps
"does this base_url already have this key?" answerable without decrypting.

The migration is exercised through the real alembic scripts against a file
database so the assertions are the same ones an operator inspecting a backup
would make: grep the raw bytes for the secret.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken

from routstr.core import vault
from routstr.core.db import UpstreamProviderRow

TEST_SECRET_KEY = "l_Tkp-7xmjcQ-IFhr6qhILrU8HPRbEmYMrfSbo_5srU="
TEST_SECRET_KEY_ALT = "_Teyrky_iToeDK51Tj1FsI9MJ340_cqKGmeher-a7MQ="

# The revision immediately before provider keys were encrypted.
PLAINTEXT_HEAD = "b4f7a1c9d2e3"
ENCRYPTED_HEAD = "a3f1c7b25e94"

LEGACY_KEY = "sk-legacy-plaintext-must-not-survive"


def _run_alembic(
    root: Path, database_url: str, command: str, revision: str, secret_key: str
) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["ROUTSTR_SECRET_KEY"] = secret_key
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _seed_plaintext_provider(
    connection: sqlite3.Connection, slug: str, api_key: str
) -> None:
    connection.execute(
        "INSERT INTO upstream_providers "
        "(slug, provider_type, base_url, api_key, enabled, provider_fee) "
        "VALUES (?, 'custom', ?, ?, 1, 1.01)",
        (slug, f"https://{slug}.example.com", api_key),
    )


def _column_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(upstream_providers)"
        ).fetchall()
    }


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_migration_encrypts_existing_plaintext_keys(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "provider-secrets.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(repo_root, database_url, "upgrade", PLAINTEXT_HEAD, TEST_SECRET_KEY)
    with sqlite3.connect(database_path) as connection:
        _seed_plaintext_provider(connection, "legacy", LEGACY_KEY)
        connection.commit()

    _run_alembic(repo_root, database_url, "upgrade", ENCRYPTED_HEAD, TEST_SECRET_KEY)

    with sqlite3.connect(database_path) as connection:
        assert "api_key" not in _column_names(connection)
        stored, fingerprint = connection.execute(
            "SELECT encrypted_api_key, api_key_fingerprint FROM upstream_providers"
        ).fetchone()

    assert vault.is_encrypted(stored)
    assert LEGACY_KEY not in stored
    assert LEGACY_KEY not in fingerprint

    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
    assert vault.decrypt(stored) == LEGACY_KEY
    assert fingerprint == vault.fingerprint(LEGACY_KEY)


def test_migrated_key_is_not_recoverable_from_the_database_file(
    repo_root: Path, tmp_path: Path
) -> None:
    database_path = tmp_path / "provider-secrets-backup.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(repo_root, database_url, "upgrade", PLAINTEXT_HEAD, TEST_SECRET_KEY)
    with sqlite3.connect(database_path) as connection:
        _seed_plaintext_provider(connection, "legacy", LEGACY_KEY)
        connection.commit()
    assert LEGACY_KEY.encode() in database_path.read_bytes()

    _run_alembic(repo_root, database_url, "upgrade", ENCRYPTED_HEAD, TEST_SECRET_KEY)
    # VACUUM is what a backup/copy tool effectively produces: freed pages, and
    # with them the old plaintext, are gone. Without it SQLite may leave the
    # pre-migration rows readable in unallocated space.
    with sqlite3.connect(database_path) as connection:
        connection.execute("VACUUM")

    assert LEGACY_KEY.encode() not in database_path.read_bytes()


def test_migration_is_idempotent(repo_root: Path, tmp_path: Path) -> None:
    database_path = tmp_path / "provider-secrets-idempotent.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(repo_root, database_url, "upgrade", PLAINTEXT_HEAD, TEST_SECRET_KEY)
    with sqlite3.connect(database_path) as connection:
        _seed_plaintext_provider(connection, "legacy", LEGACY_KEY)
        connection.commit()

    _run_alembic(repo_root, database_url, "upgrade", ENCRYPTED_HEAD, TEST_SECRET_KEY)
    _run_alembic(repo_root, database_url, "downgrade", PLAINTEXT_HEAD, TEST_SECRET_KEY)
    _run_alembic(repo_root, database_url, "upgrade", ENCRYPTED_HEAD, TEST_SECRET_KEY)

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT encrypted_api_key FROM upstream_providers"
        ).fetchall()

    assert len(rows) == 1
    os.environ["ROUTSTR_SECRET_KEY"] = TEST_SECRET_KEY
    # A second pass over already-migrated data must not encrypt the ciphertext
    # again; one decrypt has to yield the original key, not another token.
    assert vault.decrypt(rows[0][0]) == LEGACY_KEY


def test_downgrade_restores_plaintext_for_the_previous_build(
    repo_root: Path, tmp_path: Path
) -> None:
    database_path = tmp_path / "provider-secrets-downgrade.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(repo_root, database_url, "upgrade", PLAINTEXT_HEAD, TEST_SECRET_KEY)
    with sqlite3.connect(database_path) as connection:
        _seed_plaintext_provider(connection, "legacy", LEGACY_KEY)
        connection.commit()

    _run_alembic(repo_root, database_url, "upgrade", ENCRYPTED_HEAD, TEST_SECRET_KEY)
    _run_alembic(repo_root, database_url, "downgrade", PLAINTEXT_HEAD, TEST_SECRET_KEY)

    with sqlite3.connect(database_path) as connection:
        assert "encrypted_api_key" not in _column_names(connection)
        (restored,) = connection.execute(
            "SELECT api_key FROM upstream_providers"
        ).fetchone()

    assert restored == LEGACY_KEY


def test_downgrade_without_the_master_key_fails_closed(
    repo_root: Path, tmp_path: Path
) -> None:
    database_path = tmp_path / "provider-secrets-downgrade-wrong-key.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    _run_alembic(repo_root, database_url, "upgrade", PLAINTEXT_HEAD, TEST_SECRET_KEY)
    with sqlite3.connect(database_path) as connection:
        _seed_plaintext_provider(connection, "legacy", LEGACY_KEY)
        connection.commit()
    _run_alembic(repo_root, database_url, "upgrade", ENCRYPTED_HEAD, TEST_SECRET_KEY)

    with pytest.raises(subprocess.CalledProcessError):
        _run_alembic(
            repo_root, database_url, "downgrade", PLAINTEXT_HEAD, TEST_SECRET_KEY_ALT
        )

    # The failed downgrade must leave the ciphertext intact rather than write
    # back garbage the operator can never decrypt.
    with sqlite3.connect(database_path) as connection:
        (stored,) = connection.execute(
            "SELECT encrypted_api_key FROM upstream_providers"
        ).fetchone()
    assert vault.is_encrypted(stored)


def test_row_helpers_round_trip_through_the_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
    provider = UpstreamProviderRow(
        provider_type="custom", base_url="https://example.test"
    )
    provider.set_api_key("sk-live-1234")

    assert vault.is_encrypted(provider.encrypted_api_key)
    assert "sk-live-1234" not in provider.encrypted_api_key
    assert provider.decrypted_api_key() == "sk-live-1234"


def test_reading_a_row_under_the_wrong_master_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
    provider = UpstreamProviderRow(
        provider_type="custom", base_url="https://example.test"
    )
    provider.set_api_key("sk-live-1234")
    ciphertext = provider.encrypted_api_key

    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY_ALT)
    with pytest.raises(InvalidToken):
        provider.decrypted_api_key()
    assert provider.encrypted_api_key == ciphertext


def test_reading_a_row_with_no_master_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
    provider = UpstreamProviderRow(
        provider_type="custom", base_url="https://example.test"
    )
    provider.set_api_key("sk-live-1234")

    monkeypatch.delenv("ROUTSTR_SECRET_KEY")
    monkeypatch.setenv("ROUTSTR_SECRET_KEY_FILE", str(tmp_path / "absent.key"))
    with pytest.raises(RuntimeError, match="ROUTSTR_SECRET_KEY is not set"):
        provider.decrypted_api_key()


def test_stored_plaintext_is_never_accepted_as_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
    provider = UpstreamProviderRow(
        provider_type="custom",
        base_url="https://example.test",
        encrypted_api_key="sk-hand-edited-plaintext",
    )
    with pytest.raises(ValueError, match="not fernet:v1:"):
        provider.decrypted_api_key()


def test_fingerprint_identifies_a_key_without_revealing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
    same = vault.fingerprint("sk-live-1234")

    assert same == vault.fingerprint("sk-live-1234")
    assert same != vault.fingerprint("sk-live-1235")
    assert "sk-live-1234" not in same

    # Keyed, not a bare digest: without the master key an attacker holding the
    # database cannot confirm a guessed key by recomputing the fingerprint.
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY_ALT)
    assert vault.fingerprint("sk-live-1234") != same


def test_empty_key_has_no_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
    provider = UpstreamProviderRow(
        provider_type="ollama", base_url="http://localhost:11434"
    )
    provider.set_api_key("")

    assert provider.encrypted_api_key == ""
    assert provider.api_key_fingerprint == ""
    assert provider.decrypted_api_key() == ""
