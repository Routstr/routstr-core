"""Tests for ``bootstrap_secrets`` — moving node secrets into the Secret store.

Specifies the per-secret bootstrap that runs at startup (issue #553). For both
the admin password and the nsec it follows the same three branches: use the
column if already set, otherwise migrate any legacy plaintext (env first, then
the old settings blob), otherwise — admin password only — generate and log one.
A column written under a different ROUTSTR_SECRET_KEY fails fast rather than
silently corrupting state.
"""

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import pytest
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core import vault
from routstr.core.db import get_secret
from routstr.core.settings import (
    SettingsService,
    bootstrap_secrets,
    derive_npub_from_nsec,
    settings,
)

# Valid Fernet keys; must match the suite default in tests/conftest.py.
TEST_SECRET_KEY = "l_Tkp-7xmjcQ-IFhr6qhILrU8HPRbEmYMrfSbo_5srU="
TEST_SECRET_KEY_ALT = "_Teyrky_iToeDK51Tj1FsI9MJ340_cqKGmeher-a7MQ="

NSEC_HEX = "1" * 64


@pytest.fixture
def clean_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient legacy secrets, and a known in-memory settings baseline."""
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NSEC", raising=False)
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
    monkeypatch.setattr(settings, "nsec", "")
    monkeypatch.setattr(settings, "npub", "")
    monkeypatch.setattr(settings, "http_url", "")


async def _create_settings_blob(session: AsyncSession, data: dict) -> None:
    await session.exec(  # type: ignore
        text(
            "CREATE TABLE IF NOT EXISTS settings "
            "(id INTEGER PRIMARY KEY, data TEXT NOT NULL, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    )
    await session.exec(  # type: ignore
        text("INSERT INTO settings (id, data) VALUES (1, :data)").bindparams(
            data=json.dumps(data)
        )
    )
    await session.commit()


# --- admin password --------------------------------------------------------


@pytest.mark.asyncio
async def test_generates_admin_password_when_none(
    clean_secret_env: None, integration_session: AsyncSession
) -> None:
    await bootstrap_secrets(integration_session)
    secret = await get_secret(integration_session)
    assert secret.admin_password_hash is not None
    assert secret.admin_password_hash.startswith("scrypt:")


@pytest.mark.asyncio
async def test_admin_password_generation_is_idempotent(
    clean_secret_env: None, integration_session: AsyncSession
) -> None:
    await bootstrap_secrets(integration_session)
    first = (await get_secret(integration_session)).admin_password_hash
    await bootstrap_secrets(integration_session)
    second = (await get_secret(integration_session)).admin_password_hash
    assert first is not None and first == second


@pytest.mark.asyncio
async def test_hashes_legacy_admin_password_from_env(
    clean_secret_env: None,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2")
    await bootstrap_secrets(integration_session)
    secret = await get_secret(integration_session)
    assert secret.admin_password_hash is not None
    assert vault.verify_password("hunter2", secret.admin_password_hash) is True


@pytest.mark.asyncio
async def test_hashes_legacy_admin_password_from_blob(
    clean_secret_env: None, integration_session: AsyncSession
) -> None:
    # No ADMIN_PASSWORD in env, but the old settings blob carries one.
    await _create_settings_blob(integration_session, {"admin_password": "blobpw"})
    await bootstrap_secrets(integration_session)
    secret = await get_secret(integration_session)
    assert vault.verify_password("blobpw", secret.admin_password_hash or "") is True


# --- nsec ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_encrypts_legacy_nsec_from_env_and_derives_npub(
    clean_secret_env: None,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NSEC", NSEC_HEX)
    await bootstrap_secrets(integration_session)
    secret = await get_secret(integration_session)
    assert secret.encrypted_nsec is not None
    assert vault.is_encrypted(secret.encrypted_nsec) is True
    assert vault.decrypt(secret.encrypted_nsec) == NSEC_HEX
    # In-memory runtime value is the decrypted nsec, and npub is derived from it.
    assert settings.nsec == NSEC_HEX
    assert settings.npub == derive_npub_from_nsec(NSEC_HEX)


@pytest.mark.asyncio
async def test_decrypts_existing_nsec_column(
    clean_secret_env: None, integration_session: AsyncSession
) -> None:
    secret = await get_secret(integration_session)
    secret.encrypted_nsec = vault.encrypt(NSEC_HEX)
    integration_session.add(secret)
    await integration_session.commit()
    stored = secret.encrypted_nsec

    await bootstrap_secrets(integration_session)
    reloaded = await get_secret(integration_session)
    assert settings.nsec == NSEC_HEX
    # The column is reused, not re-encrypted.
    assert reloaded.encrypted_nsec == stored


@pytest.mark.asyncio
async def test_fail_fast_when_nsec_encrypted_with_different_key(
    clean_secret_env: None,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Encrypt the column under the alternate key, then bootstrap under the
    # suite key -> the value cannot be decrypted -> clear startup failure.
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY_ALT)
    secret = await get_secret(integration_session)
    secret.encrypted_nsec = vault.encrypt(NSEC_HEX)
    integration_session.add(secret)
    await integration_session.commit()

    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
    with pytest.raises(RuntimeError, match="ROUTSTR_SECRET_KEY"):
        await bootstrap_secrets(integration_session)


# --- encryption is mandatory: a node with an nsec needs ROUTSTR_SECRET_KEY -----


@pytest.mark.asyncio
async def test_legacy_nsec_without_secret_key_fails_fast(
    clean_secret_env: None,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A node with a Nostr identity must not boot without a key to encrypt it:
    # encryption at rest is mandatory, not opt-in. The failure names the missing
    # key and hands over the generation command rather than crashing opaquely or
    # silently persisting the nsec in plaintext. No env/blob copy is dropped — the
    # node refuses until the operator sets the key.
    monkeypatch.delenv("ROUTSTR_SECRET_KEY", raising=False)
    monkeypatch.setenv("NSEC", NSEC_HEX)

    with pytest.raises(RuntimeError, match="ROUTSTR_SECRET_KEY"):
        await bootstrap_secrets(integration_session)


# --- boot ordering: rescue legacy blob secrets before they are stripped ----


@pytest.mark.asyncio
async def test_blob_only_nsec_is_migrated_before_blob_is_stripped(
    clean_secret_env: None, integration_session: AsyncSession
) -> None:
    # Legacy node whose nsec lives ONLY in the settings blob (never in env).
    # bootstrap_secrets must run *before* SettingsService.initialize strips the
    # blob, or the only copy of the secret would be lost.
    await _create_settings_blob(
        integration_session, {"nsec": NSEC_HEX, "name": "LegacyNode"}
    )

    await bootstrap_secrets(integration_session)
    await SettingsService.initialize(integration_session)

    secret = await get_secret(integration_session)
    # The plaintext nsec has been moved into the encrypted Secret store...
    assert secret.encrypted_nsec is not None
    assert vault.decrypt(secret.encrypted_nsec) == NSEC_HEX
    assert settings.nsec == NSEC_HEX
    # ...and stripped from the persisted settings blob.
    row = await integration_session.exec(  # type: ignore
        text("SELECT data FROM settings WHERE id = 1")
    )
    blob = json.loads(row.first()[0])
    assert "nsec" not in blob
    assert blob["name"] == "LegacyNode"


@pytest.mark.asyncio
async def test_initialize_does_not_clobber_store_only_nsec(
    clean_secret_env: None, integration_session: AsyncSession
) -> None:
    # Steady state after migration: the nsec lives ONLY in the encrypted Secret
    # store (NSEC removed from env, blob already stripped on a previous boot).
    # bootstrap decrypts it into memory; initialize then re-derives settings from
    # the secret-free blob and must NOT wipe the live nsec back to empty (or the
    # node would silently stop signing Nostr announcements).
    await _create_settings_blob(integration_session, {"name": "LegacyNode"})
    secret = await get_secret(integration_session)
    secret.encrypted_nsec = vault.encrypt(NSEC_HEX)
    integration_session.add(secret)
    await integration_session.commit()

    await bootstrap_secrets(integration_session)
    assert settings.nsec == NSEC_HEX  # bootstrap decrypted it into memory

    await SettingsService.initialize(integration_session)
    # The live secret survives initialize even though no env/blob carries it...
    assert settings.nsec == NSEC_HEX
    # ...and is still never written back to the persisted blob.
    row = await integration_session.exec(  # type: ignore
        text("SELECT data FROM settings WHERE id = 1")
    )
    assert "nsec" not in json.loads(row.first()[0])


@pytest.mark.asyncio
async def test_startup_runs_bootstrap_before_settings_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two tests above prove the migration outcome *given* the call order;
    # they hardcode that order themselves. This one guards the order at its real
    # call site — the application lifespan — so a reorder in main.py (which would
    # strip a blob-only secret before bootstrap could rescue it) is caught.
    import routstr.core.main as main

    order: list[str] = []

    class _Abort(Exception):
        pass

    @asynccontextmanager
    async def fake_create_session() -> AsyncGenerator[None, None]:
        yield None

    async def fake_bootstrap(session: Any) -> None:
        order.append("bootstrap")

    async def fake_initialize(session: Any) -> None:
        order.append("initialize")
        # Stop startup here, before the background-task fan-out (prices, nostr,
        # upstreams) that we don't want to run in a unit test.
        raise _Abort()

    async def noop_init_db() -> None:
        return None

    monkeypatch.setattr(main, "configure_litellm", lambda: None)
    monkeypatch.setattr(main, "register_deepseek_v4_pricing", lambda: None)
    monkeypatch.setattr(main, "run_migrations", lambda: None)
    monkeypatch.setattr(main, "init_db", noop_init_db)
    monkeypatch.setattr(main, "create_session", fake_create_session)
    monkeypatch.setattr(main, "bootstrap_secrets", fake_bootstrap)
    monkeypatch.setattr(main.SettingsService, "initialize", fake_initialize)

    with pytest.raises(_Abort):
        async with main.lifespan(main.app):
            pass

    assert order == ["bootstrap", "initialize"]
