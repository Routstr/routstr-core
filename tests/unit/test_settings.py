import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.settings import SettingsService


@pytest.mark.asyncio
async def test_settings_seed_from_env_and_persist() -> None:
    os.environ["UPSTREAM_BASE_URL"] = "https://api.test/v1"
    os.environ.pop("ONION_URL", None)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        settings = await SettingsService.initialize(session)

        assert settings.upstream_base_url == "https://api.test/v1"
        # ONION_URL may be empty if not discoverable
        assert isinstance(settings.onion_url, str)


@pytest.mark.asyncio
async def test_settings_db_precedence_over_env() -> None:
    os.environ["UPSTREAM_BASE_URL"] = "https://api.env/v1"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        _ = await SettingsService.initialize(session)
        updated = await SettingsService.update({"name": "DBName"}, session)
        assert updated.name == "DBName"

        # Change env and re-initialize; DB should still win
        os.environ["NAME"] = "EnvName"
        again = await SettingsService.initialize(session)
        assert again.name == "DBName"
