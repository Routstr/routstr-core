"""Model-path discovery service.

Exposes every upstream provider path a Routstr model is reachable through.
This is discovery/visibility data only — routing still selects the cheapest or
best provider separately.

A *path* is the provider string that may appear in Routstr chat completion
responses. The strings emitted here are produced by the provider's own
``discovery_path_for_subprovider`` / ``discovery_base_paths`` hooks, which
mirror ``_apply_provider_field`` so discovery and response stamping cannot
drift:

- Direct upstream -> ``<provider_type>`` e.g. ``anthropic``
- Generic/custom OpenRouter-compatible upstream -> ``generic:<name>``
- Native OpenRouter routing to a sub-provider -> ``openrouter:<name>``
- Native OpenRouter with no usable sub-provider -> ``unknown``
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING, Callable

import httpx
from sqlalchemy import insert, or_
from sqlalchemy.orm import selectinload
from sqlmodel import col, delete, select

from ..core.db import ModelPathRow, ModelRow, UpstreamProviderRow, create_session
from ..core.logging import get_logger

if TYPE_CHECKING:
    from .base import BaseUpstreamProvider

logger = get_logger(__name__)

# Bound the per-model OpenRouter /endpoints fan-out so a provider with hundreds
# of models does not open hundreds of concurrent requests every refresh.
_OPENROUTER_CONCURRENCY = 5
_OPENROUTER_TIMEOUT_SECONDS = 10.0

# Rows inserted per statement during persist. Keeps each INSERT bounded while
# avoiding per-row round-trips that hold SQLite's write lock for ~1s per cycle.
_PERSIST_CHUNK_SIZE = 500

# Visibility key used across this module: routing carries the provider
# dimension everywhere (ModelRow's primary key is (id, upstream_provider_id)),
# so all model-id keyed maps here do too, lowercased like proxy.refresh_model_maps.
ModelKey = tuple[str, int]


def _make_http_client() -> httpx.AsyncClient:
    """Client factory, separated so tests can substitute a mock transport."""
    return httpx.AsyncClient()


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

    Uses the same rule as ``create_model_mappings.get_base_model_id`` and
    ``resolve_model_alias`` — strip everything before the *first* slash — so
    the id shown here can be sent back to ``/v1/chat/completions`` verbatim.
    """
    return model_id.split("/", 1)[1] if "/" in model_id else model_id


def openrouter_author_slug(model: object) -> str | None:
    """Return a canonical ``author/slug`` for the OpenRouter endpoints API.

    Prefer ``canonical_slug``, then a slash-containing ``id``, then a
    slash-containing ``forwarded_model_id``. The forwarded id is exactly what
    the proxy sends upstream for admin-created alias rows (``base.py`` forwards
    ``forwarded_model_id or id``), so it is a valid OpenRouter id when the
    bare ``id`` is a local alias with no slash.
    """
    canonical = getattr(model, "canonical_slug", None)
    if canonical and "/" in canonical:
        return canonical
    model_id = getattr(model, "id", None)
    if model_id and "/" in model_id:
        return model_id
    forwarded = getattr(model, "forwarded_model_id", None)
    if forwarded and "/" in forwarded:
        return forwarded
    return None


class _RefreshCycleState:
    """Per-refresh shared state: fetch dedupe cache and rate-limit latch.

    ``endpoint_cache`` dedupes byte-identical ``/endpoints`` fetches when two
    providers point at the same OpenRouter base URL. ``rate_limited`` latches
    on the first 429 so the rest of the cycle stops hammering a throttled API;
    the whole provider result then degrades to "unknown" instead of an empty
    list, which preserves previously persisted rows.
    """

    def __init__(self) -> None:
        self.endpoint_cache: dict[tuple[str, str], list[str] | None] = {}
        self.rate_limited = False


async def _fetch_openrouter_endpoint_subproviders(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    author_slug: str,
    semaphore: asyncio.Semaphore,
    cycle: _RefreshCycleState,
) -> list[str] | None:
    """Return sub-provider names for one model, or ``None`` when unknown.

    ``None`` (not ``[]``) signals a degraded fetch — network failure, rate
    limit, non-200, or an unparseable payload — so callers can distinguish
    "this model has no endpoints" from "we could not find out". Failures are
    logged and swallowed so one model never breaks the whole refresh.
    """
    cache_key = (base_url, author_slug)
    if cache_key in cycle.endpoint_cache:
        return cycle.endpoint_cache[cache_key]
    if cycle.rate_limited:
        return None

    url = f"{base_url.rstrip('/')}/models/{author_slug}/endpoints"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    result: list[str] | None
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
            cycle.endpoint_cache[cache_key] = None
            return None

    if resp.status_code == 429:
        logger.warning(
            "OpenRouter endpoint discovery rate-limited; aborting cycle",
            extra={"author_slug": author_slug},
        )
        cycle.rate_limited = True
        cycle.endpoint_cache[cache_key] = None
        return None
    if resp.status_code != 200:
        logger.warning(
            "OpenRouter endpoint discovery non-200",
            extra={"author_slug": author_slug, "status_code": resp.status_code},
        )
        cycle.endpoint_cache[cache_key] = None
        return None

    try:
        endpoints = resp.json().get("data", {}).get("endpoints", [])
        if not isinstance(endpoints, list):
            endpoints = []
        names: list[str] = []
        for endpoint in endpoints:
            provider_name = (
                endpoint.get("provider_name") if isinstance(endpoint, dict) else None
            )
            if provider_name:
                names.append(provider_name)
        result = list(dict.fromkeys(names))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "OpenRouter endpoint discovery bad payload",
            extra={"author_slug": author_slug, "error": str(e)},
        )
        result = None

    cycle.endpoint_cache[cache_key] = result
    return result


async def _load_model_visibility() -> tuple[
    dict[ModelKey, ModelRow], set[ModelKey], set[int]
]:
    """Load the same DB model visibility inputs used by routing.

    ``refresh_model_maps`` builds routing from enabled providers, enabled DB
    override rows, and disabled model keys — all keyed on
    ``(model_id.lower(), upstream_provider_id)`` because ``ModelRow``'s primary
    key is composite and the same id legitimately exists on several providers.
    Model-path discovery uses the same keying so disabling a model on one
    provider never hides it on another, and one provider's
    ``forwarded_model_id`` alias is never applied to a different provider.
    """
    async with create_session() as session:
        query = select(UpstreamProviderRow).options(
            selectinload(UpstreamProviderRow.models)  # type: ignore[arg-type]
        )
        provider_rows = (await session.exec(query)).all()

    overrides_by_key: dict[ModelKey, ModelRow] = {}
    disabled_model_keys: set[ModelKey] = set()
    enabled_provider_ids: set[int] = set()

    for provider in provider_rows:
        if not provider.enabled or provider.id is None:
            continue
        enabled_provider_ids.add(provider.id)
        for model in provider.models:
            key = (model.id.lower(), provider.id)
            if model.enabled:
                overrides_by_key[key] = model
            else:
                disabled_model_keys.add(key)

    return overrides_by_key, disabled_model_keys, enabled_provider_ids


def _apply_model_visibility(
    upstream: BaseUpstreamProvider,
    overrides_by_key: dict[ModelKey, ModelRow] | None,
    disabled_model_keys: set[ModelKey] | None,
) -> list[object]:
    """Return provider models after DB disabled/override state is applied.

    Only the identity fields (``id``, ``forwarded_model_id``,
    ``canonical_slug``) matter for path discovery, so DB override rows are used
    directly rather than rebuilt into fully priced ``Model`` objects — the
    pricing pipeline costs ~0.7ms of event-loop CPU per row for data this
    module immediately discards.
    """
    overrides_by_key = overrides_by_key or {}
    disabled_model_keys = disabled_model_keys or set()
    upstream_provider_id = getattr(upstream, "db_id", None)
    if not isinstance(upstream_provider_id, int):
        return [
            model
            for model in upstream.get_cached_models()
            if getattr(model, "enabled", True)
        ]

    visible_models: list[object] = []
    seen_model_ids: set[str] = set()

    for model in upstream.get_cached_models():
        model_id = getattr(model, "id", "")
        key = (model_id.lower(), upstream_provider_id)
        if not getattr(model, "enabled", True) or key in disabled_model_keys:
            continue
        # Apply overrides only for this provider's own model row.
        override_row = overrides_by_key.get(key)
        visible: object = model if override_row is None else override_row
        visible_models.append(visible)
        seen_model_ids.add(model_id.lower())

    # DB-only override rows for this provider with no cached counterpart.
    for (model_id_lower, provider_id), override_row in overrides_by_key.items():
        if provider_id != upstream_provider_id:
            continue
        if model_id_lower in seen_model_ids:
            continue
        visible_models.append(override_row)
        seen_model_ids.add(model_id_lower)

    return visible_models


async def _collect_provider_paths(
    upstream: BaseUpstreamProvider,
    overrides_by_key: dict[ModelKey, ModelRow] | None = None,
    disabled_model_keys: set[ModelKey] | None = None,
    cycle: _RefreshCycleState | None = None,
) -> list[tuple[str, str]] | None:
    """Collect ``(model_id, path)`` pairs for one provider instance.

    Emits the provider's ``discovery_base_paths`` for normal upstreams. For
    OpenRouter-compatible providers, additionally emits one path per OpenRouter
    sub-provider endpoint via ``discovery_path_for_subprovider`` so the strings
    match response stamping exactly.

    Returns ``None`` when the provider's path set could not be determined this
    cycle (every endpoint fetch degraded); callers must then keep previously
    persisted rows instead of wiping them.
    """
    cycle = cycle or _RefreshCycleState()
    models = _apply_model_visibility(upstream, overrides_by_key, disabled_model_keys)
    base_paths = upstream.discovery_base_paths()

    if not is_openrouter_base_url(upstream.base_url):
        return [
            (exposed_model_id(model), path) for model in models for path in base_paths
        ]

    if not (upstream.provider_type or "").strip():
        return []

    any_fetch_succeeded = False
    any_fetch_attempted = False
    semaphore = asyncio.Semaphore(_OPENROUTER_CONCURRENCY)
    async with _make_http_client() as client:

        async def _for_model(model: object) -> list[tuple[str, str]]:
            nonlocal any_fetch_succeeded, any_fetch_attempted
            model_id = exposed_model_id(model)
            # Base paths always apply: responses whose upstream payload lacks a
            # provider field are stamped with them (see _apply_provider_field).
            pairs = [(model_id, path) for path in base_paths]
            author_slug = openrouter_author_slug(model)
            if not author_slug:
                return pairs
            any_fetch_attempted = True
            sub_providers = await _fetch_openrouter_endpoint_subproviders(
                client,
                upstream.base_url,
                upstream.api_key,
                author_slug,
                semaphore,
                cycle,
            )
            if sub_providers is None:
                return []
            any_fetch_succeeded = True
            paths = [
                upstream.discovery_path_for_subprovider(name) for name in sub_providers
            ]
            pairs.extend((model_id, path) for path in paths if path)
            return list(dict.fromkeys(pairs))

        results = await asyncio.gather(
            *(_for_model(m) for m in models), return_exceptions=True
        )

    if any_fetch_attempted and not any_fetch_succeeded:
        # Every endpoint lookup degraded (offline, throttled, bad payloads):
        # the true path set is unknown, not empty.
        return None

    pairs: list[tuple[str, str]] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning(
                "OpenRouter endpoint discovery task errored",
                extra={"provider": upstream.provider_type, "error": str(result)},
            )
            continue
        pairs.extend(result)

    return pairs


async def _persist_provider_paths(
    upstream_provider_id: int, pairs: list[tuple[str, str]]
) -> None:
    """Replace all rows for ``upstream_provider_id`` with ``pairs``.

    Replacement (not upsert) so stale paths disappear when provider config or
    upstream availability changes. Rows are written with chunked bulk INSERTs
    so the transaction holds SQLite's write lock briefly — billing writes share
    this database file.
    """
    unique_pairs = list(dict.fromkeys(pairs))
    now = int(time.time())
    async with create_session() as session:
        await session.exec(  # type: ignore[call-overload]
            delete(ModelPathRow).where(
                col(ModelPathRow.upstream_provider_id) == upstream_provider_id
            )
        )
        for start in range(0, len(unique_pairs), _PERSIST_CHUNK_SIZE):
            chunk = unique_pairs[start : start + _PERSIST_CHUNK_SIZE]
            await session.execute(
                insert(ModelPathRow),
                [
                    {
                        "model_id": model_id,
                        "path": path,
                        "upstream_provider_id": upstream_provider_id,
                        "updated_at": now,
                    }
                    for model_id, path in chunk
                ],
            )
        await session.commit()


async def prune_model_paths_for_inactive_providers() -> None:
    """Delete paths whose provider is no longer enabled in the database.

    Called from ``refresh_model_maps`` so admin mutations (disable/delete
    provider) stop advertising a provider's paths immediately instead of
    waiting for the next timed refresh. Uses the DB as the source of truth, so
    it is safe at boot even before upstreams initialize.
    """
    async with create_session() as session:
        enabled_ids = (
            await session.exec(
                select(UpstreamProviderRow.id).where(
                    col(UpstreamProviderRow.enabled).is_(True)
                )
            )
        ).all()
        stmt = delete(ModelPathRow)
        if enabled_ids:
            stmt = stmt.where(
                col(ModelPathRow.upstream_provider_id).not_in(
                    [pid for pid in enabled_ids if pid is not None]
                )
            )
        await session.exec(stmt)  # type: ignore[call-overload]
        await session.commit()


async def refresh_model_paths(
    upstreams: list[BaseUpstreamProvider],
) -> None:
    """Recompute and persist model paths for every enabled provider.

    One provider's failure is logged and isolated; it must not break the rest.
    A provider whose paths could not be determined this cycle keeps its
    previously persisted rows. An empty ``upstreams`` list (e.g. a failed
    ``initialize_upstreams`` at boot) is treated as "unknown" and touches
    nothing.
    """
    if not upstreams:
        logger.warning("Skipping model paths refresh: no live upstreams")
        return

    (
        overrides_by_key,
        disabled_model_keys,
        enabled_provider_ids,
    ) = await _load_model_visibility()
    await prune_model_paths_for_inactive_providers()

    cycle = _RefreshCycleState()
    for upstream in upstreams:
        if upstream.db_id is None or upstream.db_id not in enabled_provider_ids:
            continue
        try:
            pairs = await _collect_provider_paths(
                upstream,
                overrides_by_key=overrides_by_key,
                disabled_model_keys=disabled_model_keys,
                cycle=cycle,
            )
            if pairs is None:
                logger.warning(
                    "Model paths unknown this cycle; keeping previous rows",
                    extra={
                        "provider": upstream.provider_type or upstream.base_url,
                        "db_id": upstream.db_id,
                    },
                )
                continue
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


def _refresh_interval_seconds() -> int:
    """Current interval, re-read every loop so runtime setting changes apply."""
    from ..core.settings import settings

    if not getattr(settings, "enable_model_paths_refresh", True):
        return 0
    return int(getattr(settings, "model_paths_refresh_interval_seconds", 0) or 0)


async def refresh_model_paths_periodically(
    upstreams_provider: (
        Callable[[], list[BaseUpstreamProvider]] | list[BaseUpstreamProvider]
    ),
) -> None:
    """Background task mirroring ``refresh_upstreams_models_periodically``.

    The interval and enable flag are re-read every iteration, so the refresh
    can be turned off (or on) and retuned without a restart. While disabled the
    task idles instead of exiting, so re-enabling takes effect.
    """
    _DISABLED_POLL_SECONDS = 60.0

    def _resolve_upstreams() -> list[BaseUpstreamProvider]:
        if callable(upstreams_provider):
            return upstreams_provider()
        return upstreams_provider

    while True:
        interval = _refresh_interval_seconds()
        if interval <= 0:
            try:
                await asyncio.sleep(_DISABLED_POLL_SECONDS)
            except asyncio.CancelledError:
                break
            continue

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


async def get_all_model_paths() -> dict:
    """All models with their paths, shaped for ``GET /v1/models/paths``."""
    async with create_session() as session:
        rows = (
            await session.exec(
                select(ModelPathRow).order_by(
                    col(ModelPathRow.model_id),
                    col(ModelPathRow.path),
                    col(ModelPathRow.upstream_provider_id),
                )
            )
        ).all()

    grouped: dict[str, list[dict]] = {}
    seen_paths: dict[str, set[str]] = {}
    updated_at = 0
    for row in rows:
        updated_at = max(updated_at, row.updated_at)
        model_id = public_model_id(row.model_id)
        if row.path in seen_paths.setdefault(model_id, set()):
            continue
        seen_paths[model_id].add(row.path)
        grouped.setdefault(model_id, []).append({"path": row.path})
    # Deterministic output: models sorted by public id, paths sorted within.
    data: list[dict] = []
    for grouped_model_id in sorted(grouped):
        model_paths = sorted(grouped[grouped_model_id], key=lambda p: str(p["path"]))
        data.append({"id": grouped_model_id, "paths": model_paths})
    return {"data": data, "updated_at": updated_at or None}


async def get_paths_for_model(model_id: str) -> dict:
    """Paths for a single model, shaped for ``GET /v1/models/paths/model``.

    Match by the public, unqualified model id, mirroring the model cache alias
    behavior. Both ``deepseek-v4-pro`` and ``deepseek/deepseek-v4-pro`` resolve
    every row whose stored id has the same base model id. The candidate set is
    narrowed in SQL (exact id or ``%/<id>`` suffix) so the route does not
    materialize the whole table per request.
    """
    # The request may be a full stored id ("z-ai/glm-5v-turbo") or an
    # already-stripped public id ("fireworks/models/glm-5"); accept both.
    accepted_ids = {model_id, public_model_id(model_id)}
    async with create_session() as session:
        conditions = []
        for candidate in accepted_ids:
            conditions.append(col(ModelPathRow.model_id) == candidate)
            conditions.append(col(ModelPathRow.model_id).endswith(f"/{candidate}"))
        rows = (
            await session.exec(
                select(ModelPathRow)
                .where(or_(*conditions))
                .order_by(
                    col(ModelPathRow.path),
                    col(ModelPathRow.upstream_provider_id),
                    col(ModelPathRow.model_id),
                )
            )
        ).all()

    seen: set[str] = set()
    paths: list[dict] = []
    updated_at = 0
    for row in rows:
        # The SQL suffix match is a prefilter; enforce the exact public-id rule.
        if (
            row.model_id not in accepted_ids
            and public_model_id(row.model_id) not in accepted_ids
        ):
            continue
        updated_at = max(updated_at, row.updated_at)
        if row.path in seen:
            continue
        seen.add(row.path)
        paths.append({"path": row.path})
    return {"data": paths, "updated_at": updated_at or None}
