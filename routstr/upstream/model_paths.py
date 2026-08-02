"""Model-path discovery service.

Exposes every selectable upstream route a Routstr model is reachable through.
This PR remains discovery-only: request-side routing will consume the opaque
selectors in a follow-up.

A path is a standard percent-encoded query string containing the configured
upstream URL, provider ID, client-visible model ID and, for an exact OpenRouter
endpoint, its machine-readable tag::

    url=https%3A%2F%2Fapi.anthropic.com%2Fv1&provider-id=12&model-id=claude-sonnet-4
    url=https%3A%2F%2Fopenrouter.ai%2Fapi%2Fv1&provider-id=42&model-id=claude-sonnet-4&endpoint=google-vertex%2Fus
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import selectinload
from sqlmodel import col, delete, select

from ..core.db import ModelPathRow, ModelRow, UpstreamProviderRow, create_session
from ..core.logging import get_logger

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from .base import BaseUpstreamProvider

logger = get_logger(__name__)

# Bound the per-model OpenRouter /endpoints fan-out so a provider with hundreds
# of models does not open hundreds of concurrent requests every refresh.
_OPENROUTER_CONCURRENCY = 5
_OPENROUTER_TIMEOUT_SECONDS = 10.0

# Rows inserted per statement during persist. Keeps each INSERT bounded while
# avoiding per-row round-trips that hold SQLite's write lock for ~1s per cycle.
_PERSIST_CHUNK_SIZE = 500

# Admin mutations enqueue provider IDs here instead of running OpenRouter's
# per-model endpoint fan-out inside the request. One worker serializes refreshes
# and coalesces repeated mutations for the same provider.
_scheduled_provider_refresh_ids: set[int] = set()
_scheduled_provider_refresh_task: asyncio.Task[None] | None = None

# Visibility key used across this module: routing carries the provider
# dimension everywhere (ModelRow's primary key is (id, upstream_provider_id)),
# so all model-id keyed maps here do too, lowercased like proxy.refresh_model_maps.
ModelKey = tuple[str, int]


@dataclass(frozen=True)
class EndpointIdentity:
    """Exact OpenRouter endpoint identity returned by ``/endpoints``."""

    tag: str
    provider_name: str | None


@dataclass(frozen=True)
class ConfiguredProviderIdentity:
    """Public identity of one configured upstream provider."""

    id: int
    slug: str
    provider_type: str
    base_url: str


@dataclass(frozen=True)
class DiscoveredPath:
    """One model route ready for persistence and API serialization."""

    model_id: str
    path: str
    provider: ConfiguredProviderIdentity
    endpoint_tag: str | None = None
    endpoint_name: str | None = None


@dataclass(frozen=True)
class ProviderPathSnapshot:
    """Refresh result plus model IDs whose prior rows must survive degradation."""

    paths: tuple[DiscoveredPath, ...]
    preserve_model_ids: frozenset[str] = frozenset()


def public_provider_url(base_url: str) -> str:
    """Mask private IP addresses and URLs with explicit ports."""
    parsed = urlsplit(base_url)
    try:
        if parsed.port is not None:
            return "http://localhost"
    except ValueError:
        # An invalid explicit port must not accidentally leak through.
        return "http://localhost"

    hostname = parsed.hostname
    if hostname is None:
        return base_url
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return base_url
    return "http://localhost" if address.is_private else base_url


def encode_model_path(
    base_url: str,
    provider_id: int,
    model_id: str,
    endpoint_tag: str | None = None,
) -> str:
    """Encode the complete upstream route selector advertised to clients."""
    components: list[tuple[str, str | int]] = [
        ("url", base_url),
        ("provider-id", provider_id),
        ("model-id", model_id),
    ]
    if endpoint_tag:
        components.append(("endpoint", endpoint_tag))
    return urlencode(components)


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
    """Return exactly the ID advertised by ``/v1/models``.

    A forwarded ID is already a public routable alias and must remain intact,
    including any slash. Without one, ``/v1/models`` exposes the base ID.
    """
    forwarded = getattr(model, "forwarded_model_id", None)
    if forwarded:
        return forwarded
    return public_model_id(getattr(model, "id"))


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
        self.endpoint_cache: dict[tuple[str, str], list[EndpointIdentity] | None] = {}
        self.rate_limited = False


async def _fetch_openrouter_endpoint_subproviders(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    author_slug: str,
    semaphore: asyncio.Semaphore,
    cycle: _RefreshCycleState,
) -> list[EndpointIdentity] | None:
    """Return exact endpoint identities for one model, or ``None`` when unknown.

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
    result: list[EndpointIdentity] | None
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
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        endpoints = data.get("endpoints") if isinstance(data, dict) else None
        if not isinstance(endpoints, list):
            raise ValueError("endpoints must be a list")
        identities: dict[str, EndpointIdentity] = {}
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            tag = endpoint.get("tag")
            if not isinstance(tag, str) or not tag.strip():
                continue
            provider_name = endpoint.get("provider_name")
            identities.setdefault(
                tag,
                EndpointIdentity(
                    tag=tag,
                    provider_name=provider_name
                    if isinstance(provider_name, str) and provider_name
                    else None,
                ),
            )
        if endpoints and not identities:
            raise ValueError("endpoints contain no usable tags")
        result = list(identities.values())
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "OpenRouter endpoint discovery bad payload",
            extra={"author_slug": author_slug, "error": str(e)},
        )
        result = None

    cycle.endpoint_cache[cache_key] = result
    return result


async def _load_model_visibility() -> tuple[
    dict[ModelKey, ModelRow],
    set[ModelKey],
    dict[int, ConfiguredProviderIdentity],
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
    provider_identities: dict[int, ConfiguredProviderIdentity] = {}

    for provider in provider_rows:
        if not provider.enabled or provider.id is None:
            continue
        provider_identities[provider.id] = ConfiguredProviderIdentity(
            id=provider.id,
            slug=provider.slug or f"provider-{provider.id}",
            provider_type=provider.provider_type,
            base_url=public_provider_url(provider.base_url),
        )
        for model in provider.models:
            key = (model.id.lower(), provider.id)
            if model.enabled:
                overrides_by_key[key] = model
            else:
                disabled_model_keys.add(key)

    return overrides_by_key, disabled_model_keys, provider_identities


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
    provider_identity: ConfiguredProviderIdentity,
    overrides_by_key: dict[ModelKey, ModelRow] | None = None,
    disabled_model_keys: set[ModelKey] | None = None,
    cycle: _RefreshCycleState | None = None,
) -> ProviderPathSnapshot:
    """Collect selectable routes while marking model-level degraded fetches.

    A failed OpenRouter lookup preserves only that model's prior rows. Other
    models in the same provider still refresh, so a partial outage cannot erase
    valid discovery data or freeze the entire provider snapshot.
    """
    cycle = cycle or _RefreshCycleState()
    models = _apply_model_visibility(upstream, overrides_by_key, disabled_model_keys)

    def _base_path(model: object) -> DiscoveredPath:
        model_id = exposed_model_id(model)
        return DiscoveredPath(
            model_id=model_id,
            path=encode_model_path(
                provider_identity.base_url, provider_identity.id, model_id
            ),
            provider=provider_identity,
        )

    if not is_openrouter_base_url(upstream.base_url):
        return ProviderPathSnapshot(paths=tuple(_base_path(model) for model in models))

    if not (upstream.provider_type or "").strip():
        return ProviderPathSnapshot(paths=())

    semaphore = asyncio.Semaphore(_OPENROUTER_CONCURRENCY)
    async with _make_http_client() as client:

        async def _for_model(
            model: object,
        ) -> tuple[list[DiscoveredPath], str | None]:
            model_id = exposed_model_id(model)
            author_slug = openrouter_author_slug(model)
            if not author_slug:
                return [_base_path(model)], None
            endpoints = await _fetch_openrouter_endpoint_subproviders(
                client,
                upstream.base_url,
                upstream.api_key,
                author_slug,
                semaphore,
                cycle,
            )
            if endpoints is None:
                return [], model_id
            paths = [_base_path(model)]
            paths.extend(
                DiscoveredPath(
                    model_id=model_id,
                    path=encode_model_path(
                        provider_identity.base_url,
                        provider_identity.id,
                        model_id,
                        endpoint.tag,
                    ),
                    provider=provider_identity,
                    endpoint_tag=endpoint.tag,
                    endpoint_name=endpoint.provider_name,
                )
                for endpoint in endpoints
            )
            return paths, None

        results = await asyncio.gather(
            *(_for_model(model) for model in models), return_exceptions=True
        )

    paths: list[DiscoveredPath] = []
    preserve_model_ids: set[str] = set()
    for model, result in zip(models, results):
        if isinstance(result, BaseException):
            model_id = exposed_model_id(model)
            preserve_model_ids.add(model_id)
            logger.warning(
                "OpenRouter endpoint discovery task errored",
                extra={"provider": upstream.provider_type, "error": str(result)},
            )
            continue
        model_paths, preserved_model_id = result
        paths.extend(model_paths)
        if preserved_model_id:
            preserve_model_ids.add(preserved_model_id)

    return ProviderPathSnapshot(
        paths=tuple(paths), preserve_model_ids=frozenset(preserve_model_ids)
    )


async def _persist_provider_paths(
    upstream_provider_id: int, snapshot: ProviderPathSnapshot
) -> None:
    """Replace refreshed rows while retaining model-level degraded snapshots."""
    unique_paths = list(
        {(path.model_id, path.path): path for path in snapshot.paths}.values()
    )
    now = int(time.time())
    async with create_session() as session:
        delete_stmt = delete(ModelPathRow).where(
            col(ModelPathRow.upstream_provider_id) == upstream_provider_id
        )
        if snapshot.preserve_model_ids:
            delete_stmt = delete_stmt.where(
                col(ModelPathRow.model_id).not_in(sorted(snapshot.preserve_model_ids))
            )
        await session.exec(delete_stmt)  # type: ignore[call-overload]
        for start in range(0, len(unique_paths), _PERSIST_CHUNK_SIZE):
            chunk = unique_paths[start : start + _PERSIST_CHUNK_SIZE]
            values = [
                {
                    "model_id": discovered.model_id,
                    "path": discovered.path,
                    "provider_slug": discovered.provider.slug,
                    "provider_type": discovered.provider.provider_type,
                    "endpoint_tag": discovered.endpoint_tag,
                    "endpoint_name": discovered.endpoint_name,
                    "upstream_provider_id": upstream_provider_id,
                    "updated_at": now,
                }
                for discovered in chunk
            ]
            insert_stmt = insert(ModelPathRow).values(values)
            await session.execute(
                insert_stmt.on_conflict_do_update(
                    index_elements=["model_id", "path", "upstream_provider_id"],
                    set_={
                        "provider_slug": insert_stmt.excluded.provider_slug,
                        "provider_type": insert_stmt.excluded.provider_type,
                        "endpoint_tag": insert_stmt.excluded.endpoint_tag,
                        "endpoint_name": insert_stmt.excluded.endpoint_name,
                        "updated_at": insert_stmt.excluded.updated_at,
                    },
                )
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
        provider_identities,
    ) = await _load_model_visibility()
    await prune_model_paths_for_inactive_providers()

    cycle = _RefreshCycleState()
    for upstream in upstreams:
        if upstream.db_id is None or upstream.db_id not in provider_identities:
            continue
        try:
            snapshot = await _collect_provider_paths(
                upstream,
                provider_identity=provider_identities[upstream.db_id],
                overrides_by_key=overrides_by_key,
                disabled_model_keys=disabled_model_keys,
                cycle=cycle,
            )
            if snapshot.preserve_model_ids:
                logger.warning(
                    "Some model paths are unknown; keeping their previous rows",
                    extra={
                        "provider": upstream.provider_type or upstream.base_url,
                        "db_id": upstream.db_id,
                        "preserved_models": len(snapshot.preserve_model_ids),
                    },
                )
            await _persist_provider_paths(upstream.db_id, snapshot)
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


async def refresh_model_paths_for_provider(upstream_provider_id: int) -> None:
    """Synchronize one provider when model-path discovery is enabled."""
    if _refresh_interval_seconds() <= 0:
        return

    from ..proxy import get_upstreams

    matching = [
        upstream
        for upstream in get_upstreams()
        if upstream.db_id == upstream_provider_id
    ]
    if matching:
        await refresh_model_paths(matching)
    else:
        await prune_model_paths_for_inactive_providers()


async def _drain_scheduled_provider_refreshes() -> None:
    """Serialize and coalesce model-path refreshes scheduled by admin writes."""
    global _scheduled_provider_refresh_task

    try:
        # Let mutations in the same event-loop turn collapse into one refresh.
        await asyncio.sleep(0)
        while _scheduled_provider_refresh_ids:
            if _refresh_interval_seconds() <= 0:
                _scheduled_provider_refresh_ids.clear()
                return
            provider_id = min(_scheduled_provider_refresh_ids)
            _scheduled_provider_refresh_ids.remove(provider_id)
            try:
                await refresh_model_paths_for_provider(provider_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - background best effort
                logger.warning(
                    "Failed to refresh model paths after admin mutation",
                    extra={
                        "upstream_provider_id": provider_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
    finally:
        _scheduled_provider_refresh_task = None


async def schedule_model_paths_refresh_for_provider(
    upstream_provider_id: int,
) -> None:
    """Queue a non-blocking, coalesced refresh after an admin mutation."""
    global _scheduled_provider_refresh_task

    if _refresh_interval_seconds() <= 0:
        return
    _scheduled_provider_refresh_ids.add(upstream_provider_id)
    if (
        _scheduled_provider_refresh_task is None
        or _scheduled_provider_refresh_task.done()
    ):
        _scheduled_provider_refresh_task = asyncio.create_task(
            _drain_scheduled_provider_refreshes(),
            name="model-path-admin-refresh",
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


def _serialize_path(row: ModelPathRow) -> dict[str, Any]:
    endpoint = None
    if row.endpoint_tag or row.endpoint_name:
        endpoint = {"tag": row.endpoint_tag, "name": row.endpoint_name}
    return {
        "path": row.path,
        "provider": {
            "id": row.upstream_provider_id,
            "slug": row.provider_slug,
            "type": row.provider_type,
        },
        "endpoint": endpoint,
    }


async def get_all_model_paths() -> dict:
    """All models with their exact selectable routes."""
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

    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_paths: dict[str, set[str]] = {}
    updated_at = 0
    for row in rows:
        updated_at = max(updated_at, row.updated_at)
        if row.path in seen_paths.setdefault(row.model_id, set()):
            continue
        seen_paths[row.model_id].add(row.path)
        grouped.setdefault(row.model_id, []).append(_serialize_path(row))
    data = [
        {
            "id": grouped_model_id,
            "paths": grouped[grouped_model_id],
        }
        for grouped_model_id in sorted(grouped)
    ]
    return {"data": data, "updated_at": updated_at or None}


async def get_paths_for_model(model_id: str) -> dict:
    """Return paths for an advertised ID or its provider-prefixed alias."""

    async def load_rows(session: AsyncSession, lookup_id: str) -> list[ModelPathRow]:
        return list(
            (
                await session.exec(
                    select(ModelPathRow)
                    .where(col(ModelPathRow.model_id) == lookup_id)
                    .order_by(
                        col(ModelPathRow.path),
                        col(ModelPathRow.upstream_provider_id),
                    )
                )
            ).all()
        )

    async with create_session() as session:
        rows = await load_rows(session, model_id)
        if not rows:
            unprefixed_id = public_model_id(model_id)
            if unprefixed_id != model_id:
                rows = await load_rows(session, unprefixed_id)

    seen: set[str] = set()
    paths: list[dict] = []
    updated_at = 0
    for row in rows:
        updated_at = max(updated_at, row.updated_at)
        if row.path in seen:
            continue
        seen.add(row.path)
        paths.append(_serialize_path(row))
    return {"data": paths, "updated_at": updated_at or None}
