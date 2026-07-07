import json
import os

import pytest
from pydantic.v1 import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.settings import Settings, SettingsService, settings

NSEC_HEX = "1" * 64


async def _read_settings_blob(session: AsyncSession) -> dict:
    """Return the raw persisted settings JSON (id=1) as a dict."""
    row = await session.exec(text("SELECT data FROM settings WHERE id = 1"))  # type: ignore
    return json.loads(row.first()[0])


@pytest.mark.asyncio
async def test_settings_seed_from_env_and_persist() -> None:
    os.environ["UPSTREAM_BASE_URL"] = "https://api.test/v1"
    os.environ.pop("ONION_URL", None)
    os.environ.pop("ENABLE_ANALYTICS_SHARING", None)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        settings = await SettingsService.initialize(session)

        assert settings.upstream_base_url == "https://api.test/v1"
        # ONION_URL may be empty if not discoverable
        assert isinstance(settings.onion_url, str)
        assert settings.enable_analytics_sharing is True


@pytest.mark.asyncio
async def test_settings_db_precedence_over_env() -> None:
    os.environ["UPSTREAM_BASE_URL"] = "https://api.env/v1"
    os.environ["ENABLE_ANALYTICS_SHARING"] = "true"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        _ = await SettingsService.initialize(session)
        updated = await SettingsService.update(
            {"name": "DBName", "enable_analytics_sharing": False}, session
        )
        assert updated.name == "DBName"
        assert updated.enable_analytics_sharing is False

        # Change env and re-initialize; DB should still win
        os.environ["NAME"] = "EnvName"
        os.environ["ENABLE_ANALYTICS_SHARING"] = "true"
        again = await SettingsService.initialize(session)
        assert again.name == "DBName"
        assert again.enable_analytics_sharing is False


def test_payout_settings_have_sensible_defaults() -> None:
    s = Settings()
    assert s.min_payout_sat == 210
    assert s.payout_interval_seconds == 900


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("min_payout_sat", 0),
        ("min_payout_sat", -1),
        ("payout_interval_seconds", 0),
        ("payout_interval_seconds", -10),
    ],
)
def test_payout_settings_reject_invalid_values(field: str, bad_value: int) -> None:
    kwargs: dict[str, object] = {field: bad_value}
    with pytest.raises(ValidationError):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_payout_settings_accept_custom_positive_values() -> None:
    s = Settings(min_payout_sat=500, payout_interval_seconds=60)
    assert s.min_payout_sat == 500
    assert s.payout_interval_seconds == 60


@pytest.mark.asyncio
async def test_payout_settings_persist_via_settings_service() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SettingsService.initialize(session)
        updated = await SettingsService.update(
            {"min_payout_sat": 1000, "payout_interval_seconds": 300}, session
        )
        assert updated.min_payout_sat == 1000
        assert updated.payout_interval_seconds == 300


@pytest.mark.asyncio
async def test_payout_settings_update_rejects_invalid() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SettingsService.initialize(session)
        with pytest.raises(ValidationError):
            await SettingsService.update({"min_payout_sat": 0}, session)


@pytest.mark.asyncio
async def test_settings_initialize_discards_unknown_keys() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        _ = await SettingsService.initialize(session)

        # Simulate older persisted key name and an unknown key.
        await session.exec(  # type: ignore
            text(
                "UPDATE settings SET data = :data WHERE id = 1"
            ).bindparams(
                data='{"name":"LegacyNode","nostr_analytics_enabled":false,"unknown_key":123}'
            )
        )
        await session.commit()

        reloaded = await SettingsService.initialize(session)
        assert reloaded.name == "LegacyNode"
        assert reloaded.enable_analytics_sharing is True

        row = await session.exec(text("SELECT data FROM settings WHERE id = 1"))  # type: ignore
        stored_data = row.first()[0]
        assert '"enable_analytics_sharing": true' in stored_data
        assert "nostr_analytics_enabled" not in stored_data
        assert "unknown_key" not in stored_data


# ── Secret fields are never written to the settings blob (issue #553) ────────


def test_settings_model_drops_admin_password_field() -> None:
    # admin_password now lives only as a one-way hash in the Secret store; it is
    # no longer a settings field at all.
    assert "admin_password" not in Settings.__fields__
    # nsec remains a runtime value held in memory; upstream_api_key is ordinary
    # config that still lives in the persisted blob.
    assert "nsec" in Settings.__fields__
    assert "upstream_api_key" in Settings.__fields__


@pytest.mark.asyncio
async def test_secret_fields_kept_in_memory_but_not_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # bootstrap_secrets (which runs first at boot) owns the nsec and has already
    # decrypted it into memory; simulate that live value. initialize must keep it
    # in memory for runtime consumers yet never write it to the settings blob.
    monkeypatch.setattr(settings, "nsec", NSEC_HEX)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        s = await SettingsService.initialize(session)

        # Runtime consumers still see the live secret value.
        assert s.nsec == NSEC_HEX

        # ...but it is never written to the settings blob.
        blob = await _read_settings_blob(session)
        assert "nsec" not in blob
        assert "admin_password" not in blob
        # Non-secret derived/public values are still persisted.
        assert blob["npub"] == s.npub


@pytest.mark.asyncio
async def test_upstream_api_key_survives_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # upstream_api_key is provider-scoped config, not a vault secret: it has no
    # encrypted home yet, so it must stay in the settings blob. Stripping it
    # would load it once, rewrite the blob without it, and lose it on the next
    # restart. Guard the on-disk survival path: blob-only value, no env.
    monkeypatch.delenv("UPSTREAM_API_KEY", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SettingsService.initialize(session)
        await session.exec(  # type: ignore
            text("UPDATE settings SET data = :d WHERE id = 1").bindparams(
                d=json.dumps({"name": "LegacyNode", "upstream_api_key": "sk-only-in-db"})
            )
        )
        await session.commit()

        # A reload must not drop the key from the blob...
        await SettingsService.initialize(session)
        blob = await _read_settings_blob(session)
        assert blob["upstream_api_key"] == "sk-only-in-db"
        # ...and it stays live for the proxy hot path.
        assert settings.upstream_api_key == "sk-only-in-db"


@pytest.mark.asyncio
async def test_existing_blob_secrets_are_stripped_on_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NSEC", raising=False)
    monkeypatch.delenv("UPSTREAM_API_KEY", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SettingsService.initialize(session)

        # Simulate a legacy row that still carries plaintext secrets in the blob.
        await session.exec(  # type: ignore
            text("UPDATE settings SET data = :d WHERE id = 1").bindparams(
                d=json.dumps(
                    {
                        "name": "LegacyNode",
                        "admin_password": "pw",
                        "nsec": NSEC_HEX,
                        "upstream_api_key": "sk-legacy",
                    }
                )
            )
        )
        await session.commit()

        await SettingsService.initialize(session)

        blob = await _read_settings_blob(session)
        assert "admin_password" not in blob
        assert "nsec" not in blob
        # Non-secret values survive the migration, including upstream_api_key,
        # which is not vaulted yet and so must stay in the blob.
        assert blob["name"] == "LegacyNode"
        assert blob["upstream_api_key"] == "sk-legacy"


@pytest.mark.asyncio
async def test_update_does_not_persist_secret_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NSEC", NSEC_HEX)
    monkeypatch.setattr(settings, "nsec", "")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SettingsService.initialize(session)
        await SettingsService.update({"name": "Updated"}, session)

        blob = await _read_settings_blob(session)
        assert blob["name"] == "Updated"
        assert "nsec" not in blob
        assert "admin_password" not in blob
