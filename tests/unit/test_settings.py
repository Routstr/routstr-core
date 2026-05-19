import os

import pytest
from pydantic.v1 import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import text
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.settings import Settings, SettingsService


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
            text("UPDATE settings SET data = :data WHERE id = 1").bindparams(
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
