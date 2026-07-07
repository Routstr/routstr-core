"""Tests for the model-path discovery service and endpoints."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncGenerator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.core.db import ModelRow, UpstreamProviderRow  # noqa: E402
from routstr.payment.models import models_router  # noqa: E402
from routstr.upstream import model_paths as mp  # noqa: E402

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def _model(
    id: str,
    *,
    forwarded_model_id: str | None = None,
    canonical_slug: str | None = None,
    enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        forwarded_model_id=forwarded_model_id,
        canonical_slug=canonical_slug,
        enabled=enabled,
    )


def _model_row(
    id: str,
    *,
    upstream_provider_id: int = 1,
    forwarded_model_id: str | None = None,
    canonical_slug: str | None = None,
    enabled: bool = True,
) -> ModelRow:
    return ModelRow(
        id=id,
        upstream_provider_id=upstream_provider_id,
        name=id,
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
        enabled=enabled,
        forwarded_model_id=forwarded_model_id,
        canonical_slug=canonical_slug,
    )


class _FakeProvider:
    def __init__(
        self,
        *,
        provider_type: str,
        base_url: str,
        models: list[SimpleNamespace],
        db_id: int | None = 1,
        api_key: str = "sk-test",
    ) -> None:
        self.provider_type = provider_type
        self.base_url = base_url
        self.api_key = api_key
        self.db_id = db_id
        self._models = models

    def get_cached_models(self) -> list[SimpleNamespace]:
        return self._models


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


@pytest.fixture
async def patched_session(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncEngine, None]:
    """Bind the service's ``create_session`` to a fresh in-memory engine."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    # Seed the FK target so ModelPathRow inserts satisfy the constraint.
    async with AsyncSession(engine) as session:
        for pid in (1, 2):
            session.add(
                UpstreamProviderRow(
                    id=pid,
                    slug=f"p{pid}",
                    provider_type="anthropic" if pid == 1 else "openrouter",
                    base_url=f"https://provider-{pid}",
                    api_key=f"k{pid}",
                )
            )
        await session.commit()

    @asynccontextmanager
    async def _factory() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    monkeypatch.setattr(mp, "create_session", _factory)
    yield engine
    await engine.dispose()


# --------------------------------------------------------------------------- #
# Predicates / pure helpers
# --------------------------------------------------------------------------- #


def test_is_openrouter_base_url() -> None:
    assert mp.is_openrouter_base_url("https://openrouter.ai/api/v1") is True
    assert mp.is_openrouter_base_url("https://api.anthropic.com") is False
    assert mp.is_openrouter_base_url(None) is False


def test_native_anthropic_not_openrouter() -> None:
    """Native Anthropic must not be treated as OpenRouter-compatible even though
    ``_upstream_accepts_cache_control`` returns True for it."""
    assert mp.is_openrouter_base_url("https://api.anthropic.com/v1") is False


def test_exposed_model_id_prefers_forwarded() -> None:
    assert (
        mp.exposed_model_id(_model("claude-x", forwarded_model_id="fwd-claude"))
        == "fwd-claude"
    )
    assert mp.exposed_model_id(_model("claude-x")) == "claude-x"


def test_public_model_id_strips_provider_prefix() -> None:
    assert mp.public_model_id("z-ai/glm-5v-turbo") == "glm-5v-turbo"
    assert mp.public_model_id("gpt-4o-mini") == "gpt-4o-mini"


def test_openrouter_author_slug_uses_canonical_not_forwarded() -> None:
    m = _model(
        "claude-opus-4.6",
        forwarded_model_id="forwarded-only",
        canonical_slug="anthropic/claude-opus-4.6",
    )
    assert mp.openrouter_author_slug(m) == "anthropic/claude-opus-4.6"


def test_openrouter_author_slug_falls_back_to_slash_id() -> None:
    m = _model("anthropic/claude-opus-4.6", canonical_slug="claude-opus-4.6")
    assert mp.openrouter_author_slug(m) == "anthropic/claude-opus-4.6"


def test_openrouter_author_slug_none_when_no_slash() -> None:
    m = _model("claude-opus-4.6", canonical_slug="claude-opus-4.6")
    assert mp.openrouter_author_slug(m) is None


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_direct_provider_single_path_uses_provider_type() -> None:
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("claude-opus-4.6")],
    )
    pairs = await mp._collect_provider_paths(provider)  # type: ignore[arg-type]
    assert pairs == [("claude-opus-4.6", "anthropic")]


@pytest.mark.asyncio
async def test_direct_path_stores_exposed_model_id() -> None:
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("internal-id", forwarded_model_id="claude-opus-4.6")],
    )
    pairs = await mp._collect_provider_paths(provider)  # type: ignore[arg-type]
    assert pairs == [("claude-opus-4.6", "anthropic")]


@pytest.mark.asyncio
async def test_disabled_models_excluded() -> None:
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[
            _model("enabled-model"),
            _model("disabled-model", enabled=False),
        ],
    )
    pairs = await mp._collect_provider_paths(provider)  # type: ignore[arg-type]
    assert pairs == [("enabled-model", "anthropic")]


@pytest.mark.asyncio
async def test_openrouter_provider_adds_endpoint_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider(
        provider_type="openrouter",
        base_url="https://openrouter.ai/api/v1",
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
    )

    async def _fake_get(
        self: object,
        url: str,
        headers: object = None,
        timeout: object = None,
    ) -> _FakeResponse:
        return _FakeResponse(
            200,
            {
                "data": {
                    "endpoints": [
                        {"provider_name": "Anthropic"},
                        {"provider_name": "Amazon Bedrock"},
                    ]
                }
            },
        )

    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get)

    pairs = await mp._collect_provider_paths(provider)  # type: ignore[arg-type]
    assert ("claude-opus-4.6", "openrouter:Anthropic") in pairs
    assert ("claude-opus-4.6", "openrouter:Amazon Bedrock") in pairs
    assert ("claude-opus-4.6", "openrouter") not in pairs
    assert len(pairs) == 2


@pytest.mark.asyncio
async def test_generic_provider_with_openrouter_base_url_discovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic provider pointed at OpenRouter exposes the response-stamped
    ``generic:<upstream>`` path, not a native ``openrouter:`` path."""
    provider = _FakeProvider(
        provider_type="generic",
        base_url="https://openrouter.ai/api/v1",
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
    )

    async def _fake_get(
        self: object,
        url: str,
        headers: object = None,
        timeout: object = None,
    ) -> _FakeResponse:
        return _FakeResponse(
            200, {"data": {"endpoints": [{"provider_name": "Anthropic"}]}}
        )

    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get)

    pairs = await mp._collect_provider_paths(provider)  # type: ignore[arg-type]
    assert pairs == [("claude-opus-4.6", "generic:Anthropic")]


@pytest.mark.asyncio
async def test_openrouter_failure_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider(
        provider_type="openrouter",
        base_url="https://openrouter.ai/api/v1",
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
    )

    async def _boom(
        self: object,
        url: str,
        headers: object = None,
        timeout: object = None,
    ) -> _FakeResponse:
        raise RuntimeError("network down")

    monkeypatch.setattr("httpx.AsyncClient.get", _boom)

    pairs = await mp._collect_provider_paths(provider)  # type: ignore[arg-type]
    assert pairs == []


@pytest.mark.asyncio
async def test_openrouter_rate_limit_skips_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider(
        provider_type="openrouter",
        base_url="https://openrouter.ai/api/v1",
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
    )

    async def _rate_limited(
        self: object,
        url: str,
        headers: object = None,
        timeout: object = None,
    ) -> _FakeResponse:
        return _FakeResponse(429, {})

    monkeypatch.setattr("httpx.AsyncClient.get", _rate_limited)

    pairs = await mp._collect_provider_paths(provider)  # type: ignore[arg-type]
    assert pairs == []


@pytest.mark.asyncio
async def test_openrouter_fanout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mp, "_OPENROUTER_CONCURRENCY", 3)
    models = [
        _model(f"m{i}", canonical_slug=f"author/m{i}") for i in range(20)
    ]
    provider = _FakeProvider(
        provider_type="openrouter",
        base_url="https://openrouter.ai/api/v1",
        models=models,
    )

    state = {"current": 0, "max": 0}

    async def _slow_get(
        self: object,
        url: str,
        headers: object = None,
        timeout: object = None,
    ) -> _FakeResponse:
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.02)
        state["current"] -= 1
        return _FakeResponse(200, {"data": {"endpoints": [{"provider_name": "X"}]}})

    monkeypatch.setattr("httpx.AsyncClient.get", _slow_get)

    await mp._collect_provider_paths(provider)  # type: ignore[arg-type]
    assert state["max"] <= 3, f"concurrency exceeded bound: {state['max']}"


# --------------------------------------------------------------------------- #
# Persistence + query
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_refresh_replaces_stale_rows(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(1, [("m1", "anthropic"), ("m2", "anthropic")])
    first = await mp.get_all_model_paths()
    assert {row["id"] for row in first} == {"m1", "m2"}

    # Second refresh with a different set — stale m2 must disappear.
    await mp._persist_provider_paths(1, [("m1", "anthropic")])
    second = await mp.get_all_model_paths()
    assert {row["id"] for row in second} == {"m1"}


@pytest.mark.asyncio
async def test_refresh_model_paths_excludes_db_disabled_override(
    patched_session: AsyncEngine,
) -> None:
    async with AsyncSession(patched_session) as session:
        session.add(_model_row("disabled-by-db", enabled=False))
        await session.commit()

    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("disabled-by-db")],
        db_id=1,
    )

    await mp.refresh_model_paths([provider])  # type: ignore[list-item]

    assert await mp.get_all_model_paths() == []


@pytest.mark.asyncio
async def test_refresh_model_paths_uses_db_forwarded_alias(
    patched_session: AsyncEngine,
) -> None:
    async with AsyncSession(patched_session) as session:
        session.add(_model_row("internal-id", forwarded_model_id="public-alias"))
        await session.commit()

    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("internal-id")],
        db_id=1,
    )

    await mp.refresh_model_paths([provider])  # type: ignore[list-item]

    assert await mp.get_all_model_paths() == [
        {"id": "public-alias", "paths": [{"path": "anthropic"}]}
    ]


@pytest.mark.asyncio
async def test_refresh_model_paths_includes_enabled_db_override_missing_from_cache(
    patched_session: AsyncEngine,
) -> None:
    async with AsyncSession(patched_session) as session:
        session.add(_model_row("deployment-id", forwarded_model_id="public-deployment"))
        await session.commit()

    provider = _FakeProvider(
        provider_type="generic",
        base_url="https://custom-provider/v1",
        models=[],
        db_id=1,
    )

    await mp.refresh_model_paths([provider])  # type: ignore[list-item]

    assert await mp.get_all_model_paths() == [
        {"id": "public-deployment", "paths": [{"path": "generic"}]}
    ]


@pytest.mark.asyncio
async def test_refresh_model_paths_prunes_inactive_provider_rows(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(1, [("m1", "anthropic")])
    await mp._persist_provider_paths(2, [("m2", "openrouter:Anthropic")])

    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("m1")],
        db_id=1,
    )

    await mp.refresh_model_paths([provider])  # type: ignore[list-item]
    active_only = await mp.get_all_model_paths()
    assert active_only == [{"id": "m1", "paths": [{"path": "anthropic"}]}]

    await mp.refresh_model_paths([])
    assert await mp.get_all_model_paths() == []


@pytest.mark.asyncio
async def test_refresh_model_paths_skips_disabled_db_provider(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(1, [("stale-model", "anthropic")])
    async with AsyncSession(patched_session) as session:
        provider_row = await session.get(UpstreamProviderRow, 1)
        assert provider_row is not None
        provider_row.enabled = False
        await session.commit()

    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("fresh-model")],
        db_id=1,
    )

    await mp.refresh_model_paths([provider])  # type: ignore[list-item]

    assert await mp.get_all_model_paths() == []


@pytest.mark.asyncio
async def test_same_model_two_providers_two_paths(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(1, [("claude-opus-4.6", "anthropic")])
    await mp._persist_provider_paths(2, [("claude-opus-4.6", "openrouter:Anthropic")])

    data = await mp.get_all_model_paths()
    assert len(data) == 1
    entry = data[0]
    assert entry["id"] == "claude-opus-4.6"
    paths = {p["path"] for p in entry["paths"]}
    assert paths == {"anthropic", "openrouter:Anthropic"}
    # No canonical_id anywhere.
    assert "canonical_id" not in entry
    assert all("canonical_id" not in p for p in entry["paths"])


@pytest.mark.asyncio
async def test_get_all_model_paths_deduplicates_visible_paths(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(1, [("anthropic/claude-opus-4.6", "anthropic")])
    await mp._persist_provider_paths(2, [("claude-opus-4.6", "anthropic")])

    assert await mp.get_all_model_paths() == [
        {"id": "claude-opus-4.6", "paths": [{"path": "anthropic"}]}
    ]


@pytest.mark.asyncio
async def test_get_all_model_paths_returns_unqualified_model_ids(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(4, [("z-ai/glm-5v-turbo", "openrouter:Z.AI")])
    await mp._persist_provider_paths(5, [("openai/gpt-4o-mini", "openrouter:OpenAI")])

    data = await mp.get_all_model_paths()

    assert {row["id"] for row in data} == {"glm-5v-turbo", "gpt-4o-mini"}


@pytest.mark.asyncio
async def test_get_paths_for_model_returns_only_paths(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(1, [("claude-opus-4.6", "anthropic")])
    await mp._persist_provider_paths(2, [("claude-opus-4.6", "openrouter:Anthropic")])

    paths = await mp.get_paths_for_model("claude-opus-4.6")
    assert {p["path"] for p in paths} == {"anthropic", "openrouter:Anthropic"}
    assert all(set(p.keys()) == {"path"} for p in paths)
    assert await mp.get_paths_for_model("does-not-exist") == []


@pytest.mark.asyncio
async def test_get_paths_for_model_falls_back_to_provider_prefixed_id(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(4, [("z-ai/glm-5v-turbo", "openrouter:Z.AI")])

    paths = await mp.get_paths_for_model("glm-5v-turbo")

    assert paths == [{"path": "openrouter:Z.AI"}]


@pytest.mark.asyncio
async def test_get_paths_for_model_deduplicates_visible_paths(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(1, [("anthropic/claude-opus-4.6", "anthropic")])
    await mp._persist_provider_paths(2, [("claude-opus-4.6", "anthropic")])

    assert await mp.get_paths_for_model("claude-opus-4.6") == [
        {"path": "anthropic"}
    ]
    assert await mp.get_paths_for_model("anthropic/claude-opus-4.6") == [
        {"path": "anthropic"}
    ]


@pytest.mark.asyncio
async def test_get_paths_for_model_merges_prefixed_and_unprefixed_aliases(
    patched_session: AsyncEngine,
) -> None:
    await mp._persist_provider_paths(7, [("deepseek-v4-pro", "generic")])
    await mp._persist_provider_paths(
        4, [("deepseek/deepseek-v4-pro", "openrouter:DeepSeek")]
    )

    short_paths = await mp.get_paths_for_model("deepseek-v4-pro")
    prefixed_paths = await mp.get_paths_for_model("deepseek/deepseek-v4-pro")

    assert short_paths == [
        {"path": "generic"},
        {"path": "openrouter:DeepSeek"},
    ]
    assert prefixed_paths == short_paths


@pytest.mark.asyncio
async def test_refresh_model_paths_skips_provider_without_db_id(
    patched_session: AsyncEngine,
) -> None:
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("claude-opus-4.6")],
        db_id=None,
    )
    await mp.refresh_model_paths([provider])  # type: ignore[list-item]
    assert await mp.get_all_model_paths() == []


@pytest.mark.asyncio
async def test_refresh_model_paths_isolates_provider_failure(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("claude-opus-4.6")],
        db_id=1,
    )
    bad = _FakeProvider(
        provider_type="openrouter",
        base_url="https://openrouter.ai/api/v1",
        models=[_model("m", canonical_slug="a/m")],
        db_id=2,
    )

    original = mp._collect_provider_paths

    async def _maybe_fail(
        upstream: Any, *args: Any, **kwargs: Any
    ) -> list[tuple[str, str]]:
        if upstream is bad:
            raise RuntimeError("boom")
        return await original(upstream, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mp, "_collect_provider_paths", _maybe_fail)

    await mp.refresh_model_paths([good, bad])  # type: ignore[list-item]
    data = await mp.get_all_model_paths()
    assert {row["id"] for row in data} == {"claude-opus-4.6"}


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


def _make_model_paths_app() -> FastAPI:
    app = FastAPI()
    app.include_router(models_router)
    return app


def test_model_paths_endpoint_returns_all_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get_all_model_paths() -> list[dict[str, Any]]:
        return [
            {
                "id": "claude-opus-4.6",
                "paths": [
                    {"path": "anthropic"},
                    {"path": "openrouter:Anthropic"},
                ],
            }
        ]

    monkeypatch.setattr(mp, "get_all_model_paths", _fake_get_all_model_paths)

    response = TestClient(_make_model_paths_app()).get("/v1/models/paths")

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "id": "claude-opus-4.6",
                "paths": [
                    {"path": "anthropic"},
                    {"path": "openrouter:Anthropic"},
                ],
            }
        ]
    }


def test_model_paths_for_model_endpoint_accepts_slash_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _fake_get_paths_for_model(model_id: str) -> list[dict[str, Any]]:
        calls.append(model_id)
        return [{"path": "generic:Anthropic"}]

    monkeypatch.setattr(mp, "get_paths_for_model", _fake_get_paths_for_model)

    response = TestClient(_make_model_paths_app()).get(
        "/v1/models/paths/model",
        params={"model_id": "anthropic/claude-opus-4.6"},
    )

    assert response.status_code == 200
    assert response.json() == {"data": [{"path": "generic:Anthropic"}]}
    assert calls == ["anthropic/claude-opus-4.6"]
