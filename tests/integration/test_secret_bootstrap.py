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
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core import vault
from routstr.core.db import get_secret, set_nsec
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
# A different key, standing in for a stale value left behind in env/blob after
# the vault has taken ownership of the real one.
STALE_NSEC_HEX = "2" * 64


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


# --- encryption is mandatory, key custody is not: upgrade without a key --------


@pytest.mark.asyncio
async def test_legacy_nsec_without_secret_key_generates_and_encrypts(
    clean_secret_env: None,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A node upgrading with a legacy plaintext NSEC but no ROUTSTR_SECRET_KEY must
    # NOT break. Encryption at rest stays mandatory (the nsec is never persisted
    # in plaintext), but the key custody is flexible: bootstrap generates a master
    # key, persists it to the key file, warns loudly, and encrypts the identity —
    # so the node keeps running instead of refusing to boot.
    monkeypatch.delenv("ROUTSTR_SECRET_KEY", raising=False)
    key_file = tmp_path / "routstr_secret.key"
    monkeypatch.setenv("ROUTSTR_SECRET_KEY_FILE", str(key_file))
    monkeypatch.setenv("NSEC", NSEC_HEX)

    await bootstrap_secrets(integration_session)

    # A master key was generated and persisted...
    assert key_file.exists()
    # ...the nsec is encrypted at rest under it, never stored in plaintext...
    secret = await get_secret(integration_session)
    assert secret.encrypted_nsec is not None
    assert vault.is_encrypted(secret.encrypted_nsec) is True
    assert vault.decrypt(secret.encrypted_nsec) == NSEC_HEX
    assert secret.nsec_managed is True
    # ...the node holds the live identity (npub derived from it)...
    assert settings.nsec == NSEC_HEX
    assert settings.npub == derive_npub_from_nsec(NSEC_HEX)
    # ...and the operator is loudly told a key was generated and must be backed up
    # (path + value shown) so an upgrade cannot silently create an unbacked key.
    out = capsys.readouterr().out
    assert str(key_file) in out
    assert key_file.read_text().strip() in out
    assert "BACK UP" in out.upper()


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
async def test_stale_env_nsec_does_not_override_vault_nsec(
    clean_secret_env: None,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The vault owns the nsec, but a stale NSEC (e.g. the operator rotated the
    # key in the UI yet left the old value in .env) is still in the environment.
    # bootstrap decrypts the store value; initialize must NOT let the stale env
    # value clobber it, or a restart silently reverts to the old identity.
    await set_nsec(integration_session, NSEC_HEX)

    monkeypatch.setenv("NSEC", STALE_NSEC_HEX)
    await _create_settings_blob(integration_session, {"name": "LegacyNode"})

    await bootstrap_secrets(integration_session)
    await SettingsService.initialize(integration_session)

    # The vault value wins; the stale env value is ignored.
    assert settings.nsec == NSEC_HEX


@pytest.mark.asyncio
async def test_stale_env_nsec_does_not_split_npub_from_vault_nsec(
    clean_secret_env: None,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As above, the vault owns the nsec while a stale NSEC lingers in env. The
    # private key correctly comes from the vault, but the npub must too: if
    # initialize derives the public key from the stale env nsec, the node ends up
    # with a private key from the vault and a public key from the old env value,
    # and anything reading settings.npub announces the wrong Nostr identity.
    expected_npub = derive_npub_from_nsec(NSEC_HEX)
    stale_npub = derive_npub_from_nsec(STALE_NSEC_HEX)
    assert expected_npub and stale_npub and expected_npub != stale_npub  # guard

    await set_nsec(integration_session, NSEC_HEX)

    monkeypatch.setenv("NSEC", STALE_NSEC_HEX)
    await _create_settings_blob(integration_session, {"name": "LegacyNode"})

    await bootstrap_secrets(integration_session)
    await SettingsService.initialize(integration_session)

    assert settings.nsec == NSEC_HEX
    assert settings.npub == expected_npub


@pytest.mark.asyncio
async def test_cleared_nsec_stays_cleared_across_reboot(
    clean_secret_env: None,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An identity was imported from env, then the operator cleared it via the
    # admin API. The old NSEC is still in env. On the next boot the cleared
    # identity must stay cleared, not get resurrected from the stale env value.
    monkeypatch.setenv("NSEC", NSEC_HEX)
    await bootstrap_secrets(integration_session)
    assert settings.nsec == NSEC_HEX

    # Clear via the admin path (mirrors the endpoint: store empty, live empty).
    await set_nsec(integration_session, "")
    monkeypatch.setattr(settings, "nsec", "")

    # Reboot with the stale NSEC still present in env.
    await bootstrap_secrets(integration_session)

    reloaded = await get_secret(integration_session)
    assert reloaded.nsec_managed is True
    assert reloaded.encrypted_nsec is None  # not re-imported
    assert settings.nsec == ""  # stays cleared


@pytest.mark.asyncio
async def test_initialize_keeps_npub_matching_store_only_nsec(
    clean_secret_env: None, integration_session: AsyncSession
) -> None:
    # Steady state with mandatory encryption: the nsec lives ONLY in the
    # encrypted Secret store (env carries no NSEC) and the blob has no npub.
    # bootstrap decrypts the nsec and derives the npub into memory; initialize
    # then re-derives settings from the npub-less blob and must NOT wipe the npub
    # back to empty, or the node holds a private key with no matching public key
    # and silently stops announcing a usable Nostr identity.
    expected_npub = derive_npub_from_nsec(NSEC_HEX)
    assert expected_npub  # guard: the test key must yield a real npub

    await _create_settings_blob(integration_session, {"name": "LegacyNode"})
    secret = await get_secret(integration_session)
    secret.encrypted_nsec = vault.encrypt(NSEC_HEX)
    integration_session.add(secret)
    await integration_session.commit()

    await bootstrap_secrets(integration_session)
    assert settings.npub == expected_npub  # bootstrap derived it

    await SettingsService.initialize(integration_session)
    # The npub still matches the live nsec...
    assert settings.nsec == NSEC_HEX
    assert settings.npub == expected_npub
    # ...and is persisted to the blob (it is public, not a stripped secret).
    row = await integration_session.exec(  # type: ignore
        text("SELECT data FROM settings WHERE id = 1")
    )
    assert json.loads(row.first()[0])["npub"] == expected_npub


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
