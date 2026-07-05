"""Model-path discovery service.

Exposes every upstream provider path a Routstr model is reachable through.
This is discovery/visibility data only — routing still selects the cheapest or
best provider separately.

A *path* is the provider string that may appear in Routstr chat completion
responses (see ``BaseUpstreamProvider._apply_provider_field``):

- Direct upstream -> ``<provider_type>`` e.g. ``anthropic``
- Generic/custom OpenRouter-compatible upstream -> ``generic:<name>``
- Native OpenRouter routing to a sub-provider -> ``openrouter:<name>``

Native OpenRouter does not emit a useful bare ``openrouter`` path when no
sub-provider is present; it reports ``unknown`` instead.
"""

from __future__ import annotations

import asyncio
import random
from typing import Callable

import httpx
from sqlmodel import col, delete, select

from ..core.db import ModelPathRow, create_session
from ..core.logging import get_logger
from .base import BaseUpstreamProvider

logger = get_logger(__name__)

# Bound the per-model OpenRouter /endpoints fan-out so a provider with hundreds
# of models does not open hundreds of concurrent requests every refresh.
_OPENROUTER_CONCURRENCY = 5
_OPENROUTER_TIMEOUT_SECONDS = 10.0


def is_openrouter_base_url(base_url: str | None) -> bool:
    """True when ``base_url`` points at OpenRouter.

    Deliberately separate from ``BaseUpstreamProvider._upstream_accepts_cache_control``:
    that predicate also returns True for native Anthropic (correct for
    cache-control, wrong for OpenRouter endpoint discovery). This one keys only
    on the URL so a ``GenericUpstreamProvider`` aimed at OpenRouter is matched
    while native Anthropic is not.
    """
    return "openrouter.ai" in (base_url or "")


def exposed_model_id(model: object) -> str:
    """Client-visible ``/v1/models`` id for a cached model."""
    forwarded = getattr(model, "forwarded_model_id", None)
    return forwarded or getattr(model, "id")


def public_model_id(model_id: str) -> str:
    """Model id exposed by model-path API responses.

    Provider-prefixed ids such as ``z-ai/glm-5v-turbo`` are returned as
    ``glm-5v-turbo`` so clients can search and display the same unqualified id
    they pass to ``/v1/models/paths/model``.
    """
    return model_id.rsplit("/", 1)[-1]


def openrouter_author_slug(model: object) -> str | None:
    """Return a canonical ``author/slug`` for the OpenRouter endpoints API.

    OpenRouter requires the canonical id, never ``forwarded_model_id``. Prefer
    ``canonical_slug``, then a slash-containing ``id``; otherwise there is no
    usable form and endpoint discovery is skipped for this model.
    """
    canonical = getattr(model, "canonical_slug", None)
    if canonical and "/" in canonical:
        return canonical
    model_id = getattr(model, "id", None)
    if model_id and "/" in model_id:
        return model_id
    return None


async def _fetch_openrouter_endpoint_paths(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    author_slug: str,
    path_prefix: str,
    semaphore: asyncio.Semaphore,
) -> list[str]:
    """Return ``<path_prefix>:<provider_name>`` paths for one model, or ``[]``.

    Failures (network, rate limit, bad payload) are logged and swallowed so one
    model never breaks the whole refresh.
    """
    url = f"{base_url.rstrip('/')}/models/{author_slug}/endpoints"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with semaphore:
        try:
            resp = await client.get(
                url, headers=headers, timeout=_OPENROUTER_TIMEOUT_SECONDS
            )
        except Exception as e:  # noqa: BLE001 - isolate per-model failures
            logger.warning(
                "OpenRouter endpoint discovery request failed",
                extra={"author_slug": author_slug, "error": str(e)},
            )
            return []

    if resp.status_code == 429:
        logger.warning(
            "OpenRouter endpoint discovery rate-limited",
            extra={"author_slug": author_slug},
        )
        return []
    if resp.status_code != 200:
        logger.warning(
            "OpenRouter endpoint discovery non-200",
            extra={"author_slug": author_slug, "status_code": resp.status_code},
        )
        return []

    try:
        endpoints = resp.json().get("data", {}).get("endpoints", [])
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "OpenRouter endpoint discovery bad payload",
            extra={"author_slug": author_slug, "error": str(e)},
        )
        return []

    paths: list[str] = []
    for endpoint in endpoints:
        provider_name = (endpoint or {}).get("provider_name")
        if provider_name:
            paths.append(f"{path_prefix}:{provider_name}")
    # De-duplicate while preserving order.
    return list(dict.fromkeys(paths))


async def _collect_provider_paths(
    upstream: BaseUpstreamProvider,
) -> list[tuple[str, str]]:
    """Collect ``(model_id, path)`` pairs for one provider instance.

    Emits the direct ``<provider_type>`` path for normal upstreams. For
    OpenRouter-compatible providers, emits one path per OpenRouter sub-provider
    endpoint, prefixed the same way response stamping prefixes it.
    """
    provider_type = (upstream.provider_type or "").strip()
    models = [m for m in upstream.get_cached_models() if getattr(m, "enabled", True)]
    is_openrouter = is_openrouter_base_url(upstream.base_url)

    pairs: list[tuple[str, str]] = []
    if not is_openrouter:
        for model in models:
            if provider_type:
                pairs.append((exposed_model_id(model), provider_type))
        return pairs

    if not provider_type:
        return pairs

    semaphore = asyncio.Semaphore(_OPENROUTER_CONCURRENCY)
    async with httpx.AsyncClient() as client:

        async def _for_model(model: object) -> list[tuple[str, str]]:
            author_slug = openrouter_author_slug(model)
            if not author_slug:
                return []
            paths = await _fetch_openrouter_endpoint_paths(
                client,
                upstream.base_url,
                upstream.api_key,
                author_slug,
                provider_type,
                semaphore,
            )
            model_id = exposed_model_id(model)
            return [(model_id, path) for path in paths]

        results = await asyncio.gather(
            *(_for_model(m) for m in models), return_exceptions=True
        )

    for result in results:
        if isinstance(result, BaseException):
            logger.warning(
                "OpenRouter endpoint discovery task errored",
                extra={"provider": provider_type, "error": str(result)},
            )
            continue
        pairs.extend(result)

    return pairs


async def _persist_provider_paths(
    upstream_provider_id: int, pairs: list[tuple[str, str]]
) -> None:
    """Replace all rows for ``upstream_provider_id`` with ``pairs``.

    Replacement (not upsert) so stale paths disappear when provider config or
    upstream availability changes.
    """
    unique_pairs = list(dict.fromkeys(pairs))
    async with create_session() as session:
        await session.exec(  # type: ignore[call-overload]
            delete(ModelPathRow).where(
                col(ModelPathRow.upstream_provider_id) == upstream_provider_id
            )
        )
        for model_id, path in unique_pairs:
            session.add(
                ModelPathRow(
                    model_id=model_id,
                    path=path,
                    upstream_provider_id=upstream_provider_id,
                )
            )
        await session.commit()


async def _prune_inactive_provider_paths(active_provider_ids: set[int]) -> None:
    """Delete paths for providers no longer present in the live upstream set."""
    async with create_session() as session:
        stmt = delete(ModelPathRow)
        if active_provider_ids:
            stmt = stmt.where(
                col(ModelPathRow.upstream_provider_id).not_in(active_provider_ids)
            )
        await session.exec(stmt)  # type: ignore[call-overload]
        await session.commit()


async def refresh_model_paths(
    upstreams: list[BaseUpstreamProvider],
) -> None:
    """Recompute and persist model paths for every enabled provider.

    One provider's failure is logged and isolated; it must not break the rest.
    """
    active_provider_ids = {
        upstream.db_id for upstream in upstreams if upstream.db_id is not None
    }
    await _prune_inactive_provider_paths(active_provider_ids)

    for upstream in upstreams:
        if upstream.db_id is None:
            continue
        try:
            pairs = await _collect_provider_paths(upstream)
            await _persist_provider_paths(upstream.db_id, pairs)
        except Exception as e:  # noqa: BLE001 - isolate per-provider failures
            logger.error(
                "Failed to refresh model paths for provider",
                extra={
                    "provider": upstream.provider_type or upstream.base_url,
                    "db_id": upstream.db_id,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )


async def refresh_model_paths_periodically(
    upstreams_provider: (
        Callable[[], list[BaseUpstreamProvider]] | list[BaseUpstreamProvider]
    ),
) -> None:
    """Background task mirroring ``refresh_upstreams_models_periodically``."""
    from ..core.settings import settings

    interval = getattr(settings, "model_paths_refresh_interval_seconds", 0)
    if not interval or interval <= 0:
        logger.info("Model paths refresh disabled (interval <= 0)")
        return

    def _resolve_upstreams() -> list[BaseUpstreamProvider]:
        if callable(upstreams_provider):
            return upstreams_provider()
        return upstreams_provider

    while True:
        try:
            await refresh_model_paths(_resolve_upstreams())
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Error in model paths refresh loop",
                extra={"error": str(e), "error_type": type(e).__name__},
            )

        try:
            jitter = max(0.0, float(interval) * 0.1)
            await asyncio.sleep(interval + random.uniform(0, jitter))
        except asyncio.CancelledError:
            break


async def get_all_model_paths() -> list[dict]:
    """All models with their paths, shaped for ``GET /v1/models/paths``."""
    async with create_session() as session:
        rows = (
            await session.exec(select(ModelPathRow).order_by(ModelPathRow.model_id))
        ).all()

    grouped: dict[str, list[dict]] = {}
    seen_paths: dict[str, set[str]] = {}
    for row in rows:
        model_id = public_model_id(row.model_id)
        if row.path in seen_paths.setdefault(model_id, set()):
            continue
        seen_paths[model_id].add(row.path)
        grouped.setdefault(model_id, []).append({"path": row.path})
    return [{"id": model_id, "paths": paths} for model_id, paths in grouped.items()]


async def get_paths_for_model(model_id: str) -> list[dict]:
    """Paths for a single model, shaped for ``GET /v1/models/paths/model``.

    Match by the public, unqualified model id, mirroring the model cache alias
    behavior. Both ``deepseek-v4-pro`` and ``deepseek/deepseek-v4-pro`` resolve
    every row whose stored id has the same base model id.
    """
    requested_id = public_model_id(model_id)
    async with create_session() as session:
        rows = (
            await session.exec(
                select(ModelPathRow).order_by(
                    ModelPathRow.path,
                    col(ModelPathRow.upstream_provider_id),
                    ModelPathRow.model_id,
                )
            )
        ).all()

    seen: set[str] = set()
    paths: list[dict] = []
    for row in rows:
        if public_model_id(row.model_id) != requested_id:
            continue
        if row.path in seen:
            continue
        seen.add(row.path)
        paths.append({"path": row.path})
    return paths
