"""Startup warms the models cache from stored rows instead of the upstream."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.upstream import base as base_module
from routstr.upstream import helpers as helpers_module
from routstr.upstream.base import BaseUpstreamProvider


def _model_row(model_id: str, upstream_provider_id: int = 1) -> ModelRow:
    return ModelRow(
        id=model_id,
        upstream_provider_id=upstream_provider_id,
        name=model_id,
        created=0,
        description="test model",
        context_length=8192,
        architecture=json.dumps(
            {
                "modality": "text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "test",
                "instruct_type": None,
            }
        ),
        pricing=json.dumps({"prompt": 0.000001, "completion": 0.000002}),
    )


async def _engine_with_provider(*model_ids: str) -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with AsyncSession(engine) as session:
        session.add(
            UpstreamProviderRow(
                id=1,
                slug="generic",
                provider_type="custom",
                base_url="https://upstream.test/v1",
                api_key="sk-test",
            )
        )
        for model_id in model_ids:
            session.add(_model_row(model_id))
        await session.commit()

    return engine


def _patch_create_session(
    monkeypatch: pytest.MonkeyPatch, module: Any, engine: AsyncEngine
) -> None:
    @asynccontextmanager
    async def _factory() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(module, "create_session", _factory)


class _NoNetworkProvider(BaseUpstreamProvider):
    provider_type = "custom"

    async def fetch_models(self) -> list[Any]:
        raise AssertionError("fetch_models must not run during warm start")


@pytest.mark.asyncio
async def test_load_models_cache_from_db_populates_cache_without_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _engine_with_provider("model-a", "model-b")
    _patch_create_session(monkeypatch, base_module, engine)

    provider = _NoNetworkProvider(base_url="https://upstream.test/v1", api_key="sk")
    provider.db_id = 1

    await provider.load_models_cache_from_db()

    assert sorted(m.id for m in provider.get_cached_models()) == [
        "model-a",
        "model-b",
    ]
    assert provider.get_cached_model_by_id("model-a") is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_init_upstreams_warm_start_skips_the_upstream_models_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _engine_with_provider("model-a")
    _patch_create_session(monkeypatch, helpers_module, engine)

    calls: list[str] = []

    class _Stub:
        async def refresh_models_cache(self) -> None:
            calls.append("upstream")

        async def load_models_cache_from_db(self) -> None:
            calls.append("db")

        def get_cached_models(self) -> list[Any]:
            return []

    monkeypatch.setattr(helpers_module, "_instantiate_provider", lambda row: _Stub())

    assert len(await helpers_module.init_upstreams(fetch_models=False)) == 1
    assert calls == ["db"]

    assert len(await helpers_module.init_upstreams()) == 1
    assert calls == ["db", "upstream"]

    await engine.dispose()
