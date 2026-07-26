"""Tests for the model-path discovery service and endpoints.

These tests exercise the public entry points (``refresh_model_paths``,
``get_all_model_paths``, ``get_paths_for_model``) rather than private helpers,
and fake OpenRouter at the transport level (``httpx.MockTransport``) so a
signature drift in the SUT fails loudly instead of silently returning ``[]``.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Callable, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.core.db import ModelRow, UpstreamProviderRow  # noqa: E402
from routstr.payment.models import models_router  # noqa: E402
from routstr.upstream import model_paths as mp  # noqa: E402
from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402
from routstr.upstream.openrouter import OpenRouterUpstreamProvider  # noqa: E402

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


class _FakeProvider(BaseUpstreamProvider):
    """Real ``BaseUpstreamProvider`` so the discovery-path hooks are the
    production ones, with cached models injected."""

    def __init__(
        self,
        *,
        provider_type: str,
        base_url: str,
        models: list[SimpleNamespace],
        db_id: int | None = 1,
        api_key: str = "sk-test",
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key)
        self.provider_type = provider_type  # shadow the class attribute
        self.db_id = db_id
        self._models = models

    def get_cached_models(self) -> list[SimpleNamespace]:  # type: ignore[override]
        return self._models


class _FakeOpenRouterProvider(OpenRouterUpstreamProvider):
    """Real OpenRouter provider so the ``unknown`` mapping is the production one."""

    def __init__(
        self,
        *,
        models: list[SimpleNamespace],
        db_id: int | None = 2,
        api_key: str = "sk-or",
    ) -> None:
        super().__init__(api_key=api_key)
        self.db_id = db_id
        self._models = models

    def get_cached_models(self) -> list[SimpleNamespace]:  # type: ignore[override]
        return self._models


def _mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> dict[str, int]:
    """Route the SUT's HTTP through ``httpx.MockTransport`` and count requests."""
    counter = {"requests": 0}

    def _counting_handler(request: httpx.Request) -> httpx.Response:
        counter["requests"] += 1
        return handler(request)

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_counting_handler))

    monkeypatch.setattr(mp, "_make_http_client", _factory)
    return counter


def _endpoints_response(
    *providers: str | tuple[str, str],
) -> httpx.Response:
    endpoints = []
    for provider in providers:
        if isinstance(provider, tuple):
            provider_name, tag = provider
        else:
            provider_name = provider
            tag = provider.lower().replace(" ", "-")
        endpoints.append({"provider_name": provider_name, "tag": tag})
    return httpx.Response(200, json={"data": {"endpoints": endpoints}})


_SEEDED_PROVIDER_IDS = (1, 2, 4, 5, 7)


@pytest.fixture
async def patched_session(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncEngine, None]:
    """Bind the service's ``create_session`` to a fresh in-memory engine.

    Foreign keys are enforced (``PRAGMA foreign_keys=ON``) so a ModelPathRow
    insert for an unseeded provider fails here even though production SQLite
    currently runs with the pragma off.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    # Seed every provider id the tests insert path rows for.
    async with AsyncSession(engine) as session:
        for pid in _SEEDED_PROVIDER_IDS:
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


def _paths_of(payload: dict, model_id: str) -> set[str]:
    for entry in payload["data"]:
        if entry["id"] == model_id:
            return {p["path"] for p in entry["paths"]}
    return set()


def _ids_of(payload: dict) -> set[str]:
    return {entry["id"] for entry in payload["data"]}


def _path_entry(
    provider_id: int,
    *,
    provider_slug: str | None = None,
    provider_type: str | None = None,
    endpoint_tag: str | None = None,
    endpoint_name: str | None = None,
) -> dict[str, Any]:
    endpoint = None
    if endpoint_tag or endpoint_name:
        endpoint = {"tag": endpoint_tag, "name": endpoint_name}
    return {
        "path": mp.encode_model_path(provider_id, endpoint_tag),
        "provider": {
            "id": provider_id,
            "slug": provider_slug or f"p{provider_id}",
            "type": provider_type
            or ("anthropic" if provider_id == 1 else "openrouter"),
        },
        "endpoint": endpoint,
    }


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


def test_encode_model_path_uses_provider_id_without_exposing_url() -> None:
    assert mp.encode_model_path(42) == "provider=42"
    assert mp.encode_model_path(42, "google-vertex/us-east5") == (
        "provider=42&endpoint=google-vertex%2Fus-east5"
    )


def test_exposed_model_id_prefers_forwarded() -> None:
    assert (
        mp.exposed_model_id(_model("claude-x", forwarded_model_id="fwd-claude"))
        == "fwd-claude"
    )
    assert mp.exposed_model_id(_model("claude-x")) == "claude-x"


def test_public_model_id_strips_first_provider_prefix() -> None:
    """Must match ``create_model_mappings.get_base_model_id`` (first slash),
    so the id shown by discovery can be sent to chat completions verbatim."""
    assert mp.public_model_id("z-ai/glm-5v-turbo") == "glm-5v-turbo"
    assert mp.public_model_id("gpt-4o-mini") == "gpt-4o-mini"
    assert (
        mp.public_model_id("accounts/fireworks/models/glm-5")
        == "fireworks/models/glm-5"
    )


def test_openrouter_author_slug_prefers_canonical() -> None:
    m = _model(
        "claude-opus-4.6",
        forwarded_model_id="forwarded-only",
        canonical_slug="anthropic/claude-opus-4.6",
    )
    assert mp.openrouter_author_slug(m) == "anthropic/claude-opus-4.6"


def test_openrouter_author_slug_falls_back_to_slash_id() -> None:
    m = _model("anthropic/claude-opus-4.6", canonical_slug="claude-opus-4.6")
    assert mp.openrouter_author_slug(m) == "anthropic/claude-opus-4.6"


def test_openrouter_author_slug_falls_back_to_forwarded_id() -> None:
    """Admin-created alias rows have a slash-less local id; the forwarded id is
    what the proxy actually sends to OpenRouter, so it is a usable slug."""
    m = _model("my-alias", forwarded_model_id="anthropic/claude-opus-4.6")
    assert mp.openrouter_author_slug(m) == "anthropic/claude-opus-4.6"


def test_openrouter_author_slug_none_when_no_slash() -> None:
    m = _model("claude-opus-4.6", canonical_slug="claude-opus-4.6")
    assert mp.openrouter_author_slug(m) is None


def test_discovery_paths_mirror_response_stamping() -> None:
    """The discovery hook and ``_apply_provider_field`` must agree."""
    generic = _FakeProvider(provider_type="generic", base_url="https://x", models=[])
    assert generic.discovery_path_for_subprovider("Anthropic") == "generic:Anthropic"
    assert generic.discovery_base_paths() == ["generic"]

    native = _FakeOpenRouterProvider(models=[])
    assert native.discovery_path_for_subprovider("GMICloud") == "openrouter:GMICloud"
    # Sub-provider echoing the router name is stamped "unknown" on responses.
    assert native.discovery_path_for_subprovider("OpenRouter") == "unknown"
    assert native.discovery_path_for_subprovider("openrouter:openrouter") == "unknown"
    assert native.discovery_path_for_subprovider(None) == "unknown"
    assert native.discovery_base_paths() == ["unknown"]


# --------------------------------------------------------------------------- #
# Refresh through the public entry point
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_direct_provider_single_path_uses_provider_type(
    patched_session: AsyncEngine,
) -> None:
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("claude-opus-4.6")],
        db_id=1,
    )
    await mp.refresh_model_paths([provider])
    payload = await mp.get_all_model_paths()
    assert payload["data"] == [{"id": "claude-opus-4.6", "paths": [_path_entry(1)]}]
    assert payload["updated_at"] is not None


@pytest.mark.asyncio
async def test_direct_path_stores_exposed_model_id(
    patched_session: AsyncEngine,
) -> None:
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("internal-id", forwarded_model_id="claude-opus-4.6")],
        db_id=1,
    )
    await mp.refresh_model_paths([provider])
    assert _ids_of(await mp.get_all_model_paths()) == {"claude-opus-4.6"}


@pytest.mark.asyncio
async def test_forwarded_model_id_with_slash_remains_exact_and_routable(
    patched_session: AsyncEngine,
) -> None:
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("local-alias", forwarded_model_id="anthropic/claude-opus-4.6")],
        db_id=1,
    )
    await mp.refresh_model_paths([provider])

    assert _ids_of(await mp.get_all_model_paths()) == {"anthropic/claude-opus-4.6"}
    assert (await mp.get_paths_for_model("anthropic/claude-opus-4.6"))["data"]


@pytest.mark.asyncio
async def test_disabled_cached_models_excluded(
    patched_session: AsyncEngine,
) -> None:
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("enabled-model"), _model("disabled-model", enabled=False)],
        db_id=1,
    )
    await mp.refresh_model_paths([provider])
    assert _ids_of(await mp.get_all_model_paths()) == {"enabled-model"}


@pytest.mark.asyncio
async def test_disabling_model_on_one_provider_keeps_other_provider(
    patched_session: AsyncEngine,
) -> None:
    """Regression for cross-provider isolation: ModelRow's primary key is
    (id, upstream_provider_id), so a disable row on provider 2 must not hide
    provider 1's model."""
    async with AsyncSession(patched_session) as session:
        session.add(_model_row("shared-model", upstream_provider_id=2, enabled=False))
        await session.commit()

    p1 = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("shared-model")],
        db_id=1,
    )
    p2 = _FakeProvider(
        provider_type="generic",
        base_url="https://other-upstream/v1",
        models=[_model("shared-model")],
        db_id=2,
    )

    await mp.refresh_model_paths([p1, p2])

    payload = await mp.get_all_model_paths()
    assert _paths_of(payload, "shared-model") == {mp.encode_model_path(1)}


@pytest.mark.asyncio
async def test_override_alias_not_applied_across_providers(
    patched_session: AsyncEngine,
) -> None:
    """Provider 2's forwarded_model_id must never rename provider 1's model."""
    async with AsyncSession(patched_session) as session:
        session.add(
            _model_row(
                "shared-model",
                upstream_provider_id=2,
                forwarded_model_id="private-alias",
            )
        )
        await session.commit()

    p1 = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("shared-model")],
        db_id=1,
    )
    p2 = _FakeProvider(
        provider_type="generic",
        base_url="https://other-upstream/v1",
        models=[_model("shared-model")],
        db_id=2,
    )

    await mp.refresh_model_paths([p1, p2])

    payload = await mp.get_all_model_paths()
    assert _paths_of(payload, "shared-model") == {mp.encode_model_path(1)}
    assert _paths_of(payload, "private-alias") == {mp.encode_model_path(2)}


@pytest.mark.asyncio
async def test_override_matching_is_case_insensitive(
    patched_session: AsyncEngine,
) -> None:
    """Routing lowercases both sides when matching DB rows to cached models;
    discovery must do the same for mixed-case ids."""
    async with AsyncSession(patched_session) as session:
        session.add(
            _model_row(
                "deepseek-ai/deepseek-v4-flash",
                upstream_provider_id=1,
                forwarded_model_id="public-alias",
            )
        )
        await session.commit()

    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("deepseek-ai/DeepSeek-V4-Flash")],
        db_id=1,
    )

    await mp.refresh_model_paths([provider])
    assert _ids_of(await mp.get_all_model_paths()) == {"public-alias"}


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

    await mp.refresh_model_paths([provider])

    assert (await mp.get_all_model_paths())["data"] == []


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

    await mp.refresh_model_paths([provider])

    assert (await mp.get_all_model_paths())["data"] == [
        {"id": "public-alias", "paths": [_path_entry(1)]}
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

    await mp.refresh_model_paths([provider])

    assert (await mp.get_all_model_paths())["data"] == [
        {"id": "public-deployment", "paths": [_path_entry(1)]}
    ]


@pytest.mark.asyncio
async def test_refresh_replaces_stale_rows(
    patched_session: AsyncEngine,
) -> None:
    p_two_models = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("m1"), _model("m2")],
        db_id=1,
    )
    await mp.refresh_model_paths([p_two_models])
    assert _ids_of(await mp.get_all_model_paths()) == {"m1", "m2"}

    p_one_model = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("m1")],
        db_id=1,
    )
    await mp.refresh_model_paths([p_one_model])
    assert _ids_of(await mp.get_all_model_paths()) == {"m1"}


@pytest.mark.asyncio
async def test_refresh_with_no_upstreams_keeps_existing_rows(
    patched_session: AsyncEngine,
) -> None:
    """An empty live upstream list (e.g. failed boot init) means "unknown",
    not "delete everything"."""
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("m1")],
        db_id=1,
    )
    await mp.refresh_model_paths([provider])
    assert _ids_of(await mp.get_all_model_paths()) == {"m1"}

    await mp.refresh_model_paths([])
    assert _ids_of(await mp.get_all_model_paths()) == {"m1"}


@pytest.mark.asyncio
async def test_prune_removes_rows_of_disabled_db_provider(
    patched_session: AsyncEngine,
) -> None:
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("m1")],
        db_id=1,
    )
    await mp.refresh_model_paths([provider])
    assert _ids_of(await mp.get_all_model_paths()) == {"m1"}

    async with AsyncSession(patched_session) as session:
        provider_row = await session.get(UpstreamProviderRow, 1)
        assert provider_row is not None
        provider_row.enabled = False
        session.add(provider_row)
        await session.commit()

    await mp.prune_model_paths_for_inactive_providers()
    assert (await mp.get_all_model_paths())["data"] == []


@pytest.mark.asyncio
async def test_refresh_model_paths_skips_disabled_db_provider(
    patched_session: AsyncEngine,
) -> None:
    async with AsyncSession(patched_session) as session:
        provider_row = await session.get(UpstreamProviderRow, 1)
        assert provider_row is not None
        provider_row.enabled = False
        session.add(provider_row)
        await session.commit()

    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("fresh-model")],
        db_id=1,
    )

    await mp.refresh_model_paths([provider])

    assert (await mp.get_all_model_paths())["data"] == []


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
    await mp.refresh_model_paths([provider])
    assert (await mp.get_all_model_paths())["data"] == []


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
        provider_type="generic",
        base_url="https://other-upstream/v1",
        models=[_model("m")],
        db_id=2,
    )

    original = mp._collect_provider_paths

    async def _maybe_fail(upstream: Any, *args: Any, **kwargs: Any) -> Any:
        if upstream is bad:
            raise RuntimeError("boom")
        return await original(upstream, *args, **kwargs)

    monkeypatch.setattr(mp, "_collect_provider_paths", _maybe_fail)

    await mp.refresh_model_paths([good, bad])
    assert _ids_of(await mp.get_all_model_paths()) == {"claude-opus-4.6"}


# --------------------------------------------------------------------------- #
# OpenRouter endpoint discovery (transport-level fakes)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openrouter_provider_adds_endpoint_paths(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeOpenRouterProvider(
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
        db_id=2,
    )
    _mock_transport(
        monkeypatch,
        lambda request: _endpoints_response(
            ("Google", "google-vertex/eu"),
            ("Google", "google-vertex/us"),
        ),
    )

    await mp.refresh_model_paths([provider])

    payload = await mp.get_paths_for_model("claude-opus-4.6")
    assert {item["path"] for item in payload["data"]} == {
        mp.encode_model_path(2),
        mp.encode_model_path(2, "google-vertex/eu"),
        mp.encode_model_path(2, "google-vertex/us"),
    }
    assert {
        item["endpoint"]["tag"] for item in payload["data"] if item["endpoint"]
    } == {"google-vertex/eu", "google-vertex/us"}
    assert {
        item["endpoint"]["name"] for item in payload["data"] if item["endpoint"]
    } == {"Google"}
    assert {item["provider"]["id"] for item in payload["data"]} == {2}


@pytest.mark.asyncio
async def test_openrouter_uses_exact_tag_even_when_display_name_is_router(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Machine-readable endpoint tags, not display names, define identity."""
    provider = _FakeOpenRouterProvider(
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
        db_id=2,
    )
    _mock_transport(monkeypatch, lambda request: _endpoints_response("OpenRouter"))

    await mp.refresh_model_paths([provider])

    paths = _paths_of(await mp.get_all_model_paths(), "claude-opus-4.6")
    assert paths == {mp.encode_model_path(2), mp.encode_model_path(2, "openrouter")}


@pytest.mark.asyncio
async def test_generic_provider_with_openrouter_base_url_discovers(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured provider identity is independent from its endpoint URL."""
    provider = _FakeProvider(
        provider_type="generic",
        base_url="https://openrouter.ai/api/v1",
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
        db_id=1,
    )
    _mock_transport(monkeypatch, lambda request: _endpoints_response("Anthropic"))

    await mp.refresh_model_paths([provider])

    paths = _paths_of(await mp.get_all_model_paths(), "claude-opus-4.6")
    assert paths == {mp.encode_model_path(1), mp.encode_model_path(1, "anthropic")}


@pytest.mark.asyncio
async def test_openrouter_partial_failure_keeps_failed_models_previous_rows(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeOpenRouterProvider(
        models=[
            _model("good", canonical_slug="author/good"),
            _model("degraded", canonical_slug="author/degraded"),
        ],
        db_id=2,
    )
    _mock_transport(monkeypatch, lambda request: _endpoints_response("Anthropic"))
    await mp.refresh_model_paths([provider])
    before = _paths_of(await mp.get_all_model_paths(), "degraded")
    assert before

    def _partial_failure(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/author/degraded/endpoints"):
            return httpx.Response(503)
        return _endpoints_response("Google")

    _mock_transport(monkeypatch, _partial_failure)
    await mp.refresh_model_paths([provider])

    assert _paths_of(await mp.get_all_model_paths(), "degraded") == before
    assert _paths_of(await mp.get_all_model_paths(), "good") != before


@pytest.mark.asyncio
async def test_openrouter_failure_keeps_previous_rows(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient upstream failure means the path set is unknown; previously
    persisted rows must survive, mirroring ``refresh_models_cache``."""
    provider = _FakeOpenRouterProvider(
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
        db_id=2,
    )
    _mock_transport(monkeypatch, lambda request: _endpoints_response("Anthropic"))
    await mp.refresh_model_paths([provider])
    before = _paths_of(await mp.get_all_model_paths(), "claude-opus-4.6")
    assert mp.encode_model_path(2, "anthropic") in before

    def _network_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    _mock_transport(monkeypatch, _network_down)
    await mp.refresh_model_paths([provider])

    after = _paths_of(await mp.get_all_model_paths(), "claude-opus-4.6")
    assert after == before


@pytest.mark.asyncio
async def test_openrouter_rate_limit_aborts_cycle_and_keeps_rows(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first 429 latches: no further endpoint requests this cycle, and the
    provider's previously persisted rows survive."""
    models = [_model(f"m{i}", canonical_slug=f"author/m{i}") for i in range(10)]
    provider = _FakeOpenRouterProvider(models=models, db_id=2)

    _mock_transport(monkeypatch, lambda request: _endpoints_response("Anthropic"))
    await mp.refresh_model_paths([provider])
    expected = {mp.encode_model_path(2), mp.encode_model_path(2, "anthropic")}
    assert _paths_of(await mp.get_all_model_paths(), "m0") == expected

    counter = _mock_transport(monkeypatch, lambda request: httpx.Response(429))
    await mp.refresh_model_paths([provider])

    # Up to _OPENROUTER_CONCURRENCY requests may already be in flight when the
    # first 429 lands; the latch must stop everything after that.
    assert counter["requests"] <= mp._OPENROUTER_CONCURRENCY, (
        "429 must abort the remaining fan-out"
    )
    assert _paths_of(await mp.get_all_model_paths(), "m0") == expected


@pytest.mark.asyncio
async def test_openrouter_bad_payload_shapes_preserve_previous_rows(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed successful responses are degraded snapshots, not empty sets."""
    provider = _FakeOpenRouterProvider(
        models=[_model("m", canonical_slug="a/m")], db_id=2
    )
    _mock_transport(monkeypatch, lambda request: _endpoints_response("Anthropic"))
    await mp.refresh_model_paths([provider])
    before = _paths_of(await mp.get_all_model_paths(), "m")

    for payload in (
        {"data": {"endpoints": None}},
        {"data": {"endpoints": "none"}},
        {"data": {"endpoints": [{"provider_name": "Anthropic"}]}},
        {"data": None},
        {},
    ):

        def _handler(
            request: httpx.Request, p: dict[str, Any] | None = payload
        ) -> httpx.Response:
            return httpx.Response(200, json=p)

        _mock_transport(monkeypatch, _handler)
        await mp.refresh_model_paths([provider])
        assert _paths_of(await mp.get_all_model_paths(), "m") == before


@pytest.mark.asyncio
async def test_openrouter_shared_base_url_fetched_once(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two providers on the same OpenRouter base URL share the per-cycle
    endpoint cache instead of fetching byte-identical bodies twice."""
    native = _FakeOpenRouterProvider(
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
        db_id=2,
    )
    generic = _FakeProvider(
        provider_type="generic",
        base_url="https://openrouter.ai/api/v1",
        models=[_model("claude-opus-4.6", canonical_slug="anthropic/claude-opus-4.6")],
        db_id=4,
    )
    counter = _mock_transport(
        monkeypatch, lambda request: _endpoints_response("Anthropic")
    )

    await mp.refresh_model_paths([native, generic])

    assert counter["requests"] == 1
    paths = _paths_of(await mp.get_all_model_paths(), "claude-opus-4.6")
    assert paths == {
        mp.encode_model_path(2),
        mp.encode_model_path(2, "anthropic"),
        mp.encode_model_path(4),
        mp.encode_model_path(4, "anthropic"),
    }


@pytest.mark.asyncio
async def test_openrouter_fanout_is_bounded(
    patched_session: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mp, "_OPENROUTER_CONCURRENCY", 3)
    models = [_model(f"m{i}", canonical_slug=f"author/m{i}") for i in range(20)]
    provider = _FakeOpenRouterProvider(models=models, db_id=2)

    state = {"current": 0, "max": 0}

    async def _slow_handler(request: httpx.Request) -> httpx.Response:
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.02)
        state["current"] -= 1
        return _endpoints_response("X")

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_slow_handler))

    monkeypatch.setattr(mp, "_make_http_client", _factory)

    await mp.refresh_model_paths([provider])
    assert state["max"] > 0, "transport fake was never exercised"
    assert state["max"] <= 3, f"concurrency exceeded bound: {state['max']}"


# --------------------------------------------------------------------------- #
# Query endpoints
# --------------------------------------------------------------------------- #


async def _seed_two_provider_shared_model(engine: AsyncEngine) -> None:
    p1 = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("claude-opus-4.6")],
        db_id=1,
    )
    p2 = _FakeProvider(
        provider_type="generic",
        base_url="https://other/v1",
        models=[_model("claude-opus-4.6")],
        db_id=2,
    )
    await mp.refresh_model_paths([p1, p2])


@pytest.mark.asyncio
async def test_same_model_two_providers_two_paths(
    patched_session: AsyncEngine,
) -> None:
    await _seed_two_provider_shared_model(patched_session)

    payload = await mp.get_all_model_paths()
    assert len(payload["data"]) == 1
    entry = payload["data"][0]
    assert entry["id"] == "claude-opus-4.6"
    assert {p["path"] for p in entry["paths"]} == {
        mp.encode_model_path(1),
        mp.encode_model_path(2),
    }
    assert "canonical_id" not in entry
    assert all("canonical_id" not in p for p in entry["paths"])


@pytest.mark.asyncio
async def test_get_all_model_paths_keeps_distinct_configured_providers(
    patched_session: AsyncEngine,
) -> None:
    p1 = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("anthropic/claude-opus-4.6")],
        db_id=1,
    )
    p2 = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("claude-opus-4.6")],
        db_id=2,
    )
    await mp.refresh_model_paths([p1, p2])

    assert (await mp.get_all_model_paths())["data"] == [
        {
            "id": "claude-opus-4.6",
            "paths": [_path_entry(1), _path_entry(2)],
        }
    ]


@pytest.mark.asyncio
async def test_get_all_model_paths_is_deterministic(
    patched_session: AsyncEngine,
) -> None:
    """Output must not depend on rowid insertion order, which changes every
    refresh cycle."""
    provider = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("b-model"), _model("a-model")],
        db_id=1,
    )
    await mp.refresh_model_paths([provider])
    first = await mp.get_all_model_paths()
    await mp.refresh_model_paths([provider])
    second = await mp.get_all_model_paths()
    assert first["data"] == second["data"]
    assert [e["id"] for e in first["data"]] == ["a-model", "b-model"]


@pytest.mark.asyncio
async def test_get_paths_for_model_returns_route_identity(
    patched_session: AsyncEngine,
) -> None:
    await _seed_two_provider_shared_model(patched_session)

    payload = await mp.get_paths_for_model("claude-opus-4.6")
    assert payload["data"] == [_path_entry(1), _path_entry(2)]
    assert (await mp.get_paths_for_model("does-not-exist"))["data"] == []


@pytest.mark.asyncio
async def test_get_paths_for_model_falls_back_to_provider_prefixed_id(
    patched_session: AsyncEngine,
) -> None:
    provider = _FakeProvider(
        provider_type="generic",
        base_url="https://x/v1",
        models=[_model("z-ai/glm-5v-turbo")],
        db_id=4,
    )
    await mp.refresh_model_paths([provider])

    assert (await mp.get_paths_for_model("glm-5v-turbo"))["data"] == [_path_entry(4)]


@pytest.mark.asyncio
async def test_get_paths_for_model_requires_exact_advertised_id(
    patched_session: AsyncEngine,
) -> None:
    p1 = _FakeProvider(
        provider_type="generic",
        base_url="https://x/v1",
        models=[_model("deepseek-v4-pro")],
        db_id=7,
    )
    p2 = _FakeProvider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=[_model("deepseek/deepseek-v4-pro")],
        db_id=4,
    )
    await mp.refresh_model_paths([p1, p2])

    short_paths = (await mp.get_paths_for_model("deepseek-v4-pro"))["data"]
    prefixed_paths = (await mp.get_paths_for_model("deepseek/deepseek-v4-pro"))["data"]

    assert short_paths == [_path_entry(4), _path_entry(7)]
    assert prefixed_paths == []


@pytest.mark.asyncio
async def test_get_paths_for_model_multi_segment_id_matches_models_listing(
    patched_session: AsyncEngine,
) -> None:
    """For three-segment ids the discovery id must be the same base id the
    rest of the system exposes (first-slash rule), not the last segment."""
    provider = _FakeProvider(
        provider_type="generic",
        base_url="https://x/v1",
        models=[_model("accounts/fireworks/models/glm-5")],
        db_id=1,
    )
    await mp.refresh_model_paths([provider])

    assert _ids_of(await mp.get_all_model_paths()) == {"fireworks/models/glm-5"}
    assert (await mp.get_paths_for_model("fireworks/models/glm-5"))["data"] == [
        _path_entry(1)
    ]
    assert (await mp.get_paths_for_model("accounts/fireworks/models/glm-5"))[
        "data"
    ] == []


# --------------------------------------------------------------------------- #
# Immediate and periodic refresh
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_refresh_model_paths_for_provider_selects_mutated_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routstr.proxy as proxy

    target = SimpleNamespace(db_id=2)
    other = SimpleNamespace(db_id=1)
    seen: list[list[Any]] = []

    monkeypatch.setattr(proxy, "get_upstreams", lambda: [other, target])

    async def _fake_refresh(upstreams: list[Any]) -> None:
        seen.append(upstreams)

    monkeypatch.setattr(mp, "refresh_model_paths", _fake_refresh)

    await mp.refresh_model_paths_for_provider(2)

    assert seen == [[target]]


@pytest.mark.asyncio
async def test_refresh_loop_rereads_interval_and_picks_up_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "enable_model_paths_refresh", True, raising=False)
    monkeypatch.setattr(
        settings, "model_paths_refresh_interval_seconds", 1, raising=False
    )

    seen_batches: list[list[Any]] = []

    async def _fake_refresh(upstreams: list[Any]) -> None:
        seen_batches.append(list(upstreams))

    monkeypatch.setattr(mp, "refresh_model_paths", _fake_refresh)

    sleeps: list[float] = []

    async def _fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(seen_batches) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(mp.asyncio, "sleep", _fast_sleep)

    batches = [["p1"], ["p1", "p2"]]

    def _provider() -> list[Any]:
        return batches[min(len(seen_batches), len(batches) - 1)]

    await mp.refresh_model_paths_periodically(_provider)  # type: ignore[arg-type]

    assert seen_batches[0] == ["p1"]
    assert seen_batches[1] == ["p1", "p2"], "loop must re-resolve upstreams each cycle"
    assert all(s >= 1 for s in sleeps)


@pytest.mark.asyncio
async def test_refresh_loop_idles_while_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive interval (or the kill switch) must idle the loop, not
    exit it, so runtime re-enabling takes effect."""
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "enable_model_paths_refresh", False, raising=False)
    monkeypatch.setattr(
        settings, "model_paths_refresh_interval_seconds", 600, raising=False
    )

    refresh_calls: list[Any] = []

    async def _fake_refresh(upstreams: list[Any]) -> None:
        refresh_calls.append(upstreams)

    monkeypatch.setattr(mp, "refresh_model_paths", _fake_refresh)

    idle_sleeps: list[float] = []

    async def _fast_sleep(seconds: float) -> None:
        idle_sleeps.append(seconds)
        if len(idle_sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(mp.asyncio, "sleep", _fast_sleep)

    def _upstreams() -> list[BaseUpstreamProvider]:
        return [cast(BaseUpstreamProvider, object())]

    await mp.refresh_model_paths_periodically(_upstreams)

    assert refresh_calls == [], "disabled loop must not refresh"
    assert len(idle_sleeps) == 2, "disabled loop must keep polling, not exit"


def test_refresh_interval_respects_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "enable_model_paths_refresh", False, raising=False)
    monkeypatch.setattr(
        settings, "model_paths_refresh_interval_seconds", 600, raising=False
    )
    assert mp._refresh_interval_seconds() == 0

    monkeypatch.setattr(settings, "enable_model_paths_refresh", True, raising=False)
    assert mp._refresh_interval_seconds() == 600


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #


def _make_model_paths_app() -> FastAPI:
    app = FastAPI()
    app.include_router(models_router)
    return app


def test_model_paths_endpoint_returns_all_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "data": [
            {
                "id": "claude-opus-4.6",
                "paths": [
                    _path_entry(1),
                    _path_entry(
                        2,
                        endpoint_tag="google-vertex/us",
                        endpoint_name="Google",
                    ),
                ],
            }
        ],
        "updated_at": 1753500000,
    }

    async def _fake_get_all_model_paths() -> dict[str, Any]:
        return expected

    monkeypatch.setattr(mp, "get_all_model_paths", _fake_get_all_model_paths)

    response = TestClient(_make_model_paths_app()).get("/v1/models/paths")

    assert response.status_code == 200
    assert response.json() == expected


def test_model_paths_for_model_returns_404_for_unknown_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routstr.proxy as proxy

    async def _fake_get_paths_for_model(model_id: str) -> dict[str, Any]:
        return {"data": [], "updated_at": None}

    monkeypatch.setattr(mp, "get_paths_for_model", _fake_get_paths_for_model)
    monkeypatch.setattr(proxy, "get_unique_models", lambda: [])

    response = TestClient(_make_model_paths_app()).get(
        "/v1/models/paths/model", params={"model_id": "does-not-exist"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Model not found"}


def test_model_paths_for_known_model_can_return_empty_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routstr.proxy as proxy

    async def _fake_get_paths_for_model(model_id: str) -> dict[str, Any]:
        return {"data": [], "updated_at": None}

    monkeypatch.setattr(mp, "get_paths_for_model", _fake_get_paths_for_model)
    monkeypatch.setattr(proxy, "get_unique_models", lambda: [_model("known")])

    response = TestClient(_make_model_paths_app()).get(
        "/v1/models/paths/model", params={"model_id": "known"}
    )

    assert response.status_code == 200
    assert response.json() == {"data": [], "updated_at": None}


def test_model_paths_for_routing_only_alias_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routstr.proxy as proxy

    async def _fake_get_paths_for_model(model_id: str) -> dict[str, Any]:
        return {"data": [], "updated_at": None}

    monkeypatch.setattr(mp, "get_paths_for_model", _fake_get_paths_for_model)
    monkeypatch.setattr(proxy, "get_unique_models", lambda: [_model("advertised")])

    response = TestClient(_make_model_paths_app()).get(
        "/v1/models/paths/model", params={"model_id": "routing-alias"}
    )

    assert response.status_code == 404


def test_model_paths_for_model_endpoint_accepts_slash_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    expected = {
        "data": [_path_entry(2, endpoint_tag="anthropic", endpoint_name="Anthropic")],
        "updated_at": None,
    }

    async def _fake_get_paths_for_model(model_id: str) -> dict[str, Any]:
        calls.append(model_id)
        return expected

    monkeypatch.setattr(mp, "get_paths_for_model", _fake_get_paths_for_model)

    response = TestClient(_make_model_paths_app()).get(
        "/v1/models/paths/model",
        params={"model_id": "anthropic/claude-opus-4.6"},
    )

    assert response.status_code == 200
    assert response.json() == expected
    assert calls == ["anthropic/claude-opus-4.6"]
