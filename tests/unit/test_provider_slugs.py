from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import UpstreamProviderRow
from routstr.core.provider_slugs import (
    allocate_unique_provider_slug,
    provider_slug_base,
)
from routstr.upstream.helpers import _seed_providers_from_settings


@pytest.mark.asyncio
async def test_allocate_unique_provider_slug_is_deterministic_with_suffixes() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(engine) as session:
        session.add(
            UpstreamProviderRow(
                slug="openai",
                provider_type="openai",
                base_url="https://api.openai.com/v1",
                api_key="key-1",
            )
        )
        await session.commit()

        assert await allocate_unique_provider_slug(session, "openai") == "openai-2"
        assert (
            await allocate_unique_provider_slug(session, "openai", {"openai-2"})
            == "openai-3"
        )

    await engine.dispose()


def test_provider_slug_base_sanitizes_provider_type() -> None:
    assert provider_slug_base("OpenAI Compatible") == "openai-compatible"
    assert provider_slug_base("!!!") == "provider"
    assert provider_slug_base("AI") == "ai-provider"
    assert provider_slug_base("123") == "provider-123"


@pytest.mark.asyncio
async def test_seed_providers_from_settings_sets_deterministic_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    monkeypatch.setenv("OPENAI_API_KEY", "seeded-openai-key")

    class SettingsStub:
        chat_completions_api_version: str | None = None
        upstream_base_url: str | None = None
        upstream_api_key: str = ""

    async with AsyncSession(engine) as session:
        await _seed_providers_from_settings(session, SettingsStub())  # type: ignore[arg-type]
        await session.commit()

        result = await session.exec(select(UpstreamProviderRow))
        providers: list[UpstreamProviderRow] = list(result.all())

    assert [(p.provider_type, p.slug) for p in providers] == [("openai", "openai")]

    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_providers_from_settings_keeps_slug_stable_on_reseed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    monkeypatch.setenv("OPENAI_API_KEY", "seeded-openai-key")

    class SettingsStub:
        chat_completions_api_version: str | None = None
        upstream_base_url: str | None = None
        upstream_api_key: str = ""

    async with AsyncSession(engine) as session:
        session.add(
            UpstreamProviderRow(
                slug="openai",
                provider_type="openai",
                base_url="https://example.invalid/v1",
                api_key="other-key",
            )
        )
        await session.commit()

        await _seed_providers_from_settings(session, SettingsStub())  # type: ignore[arg-type]
        await session.commit()
        await _seed_providers_from_settings(session, SettingsStub())  # type: ignore[arg-type]
        await session.commit()

        result = await session.exec(
            select(UpstreamProviderRow).order_by(UpstreamProviderRow.slug)
        )
        providers: list[UpstreamProviderRow] = list(result.all())

    assert [(p.provider_type, p.slug) for p in providers] == [
        ("openai", "openai"),
        ("openai", "openai-2"),
    ]

    await engine.dispose()
