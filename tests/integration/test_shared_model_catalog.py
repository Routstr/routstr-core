"""Workers share each provider's model catalog through the database.

Every worker used to keep only the catalog it fetched itself. One failed or
rate-limited fetch left it answering "model not found" while its siblings served
the full list, which behind a load balancer failed most requests.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import UpstreamModelSnapshot, UpstreamProviderRow
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.base import BaseUpstreamProvider


def _model(model_id: str) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=1,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="x",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.000001, completion=0.000002),
    )


CATALOG = [_model("alpha"), _model("beta")]


async def _provider_row(session: AsyncSession, api_key: str = "k") -> int:
    row = UpstreamProviderRow(
        provider_type="custom", base_url="https://upstream.example/v1", api_key=api_key
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    assert row.id is not None
    return row.id


def _worker(provider_id: int, fetched: list[Model], api_key: str = "k") -> Any:
    """One worker's provider instance whose upstream fetch returns ``fetched``."""
    provider = BaseUpstreamProvider("https://upstream.example/v1", api_key)
    provider.provider_type = "custom"
    provider.db_id = provider_id
    provider.fetch_models = AsyncMock(return_value=fetched)  # type: ignore[method-assign]
    return provider


def _ids(provider: BaseUpstreamProvider) -> set[str]:
    return {m.id for m in provider.get_cached_models()}


@pytest.fixture
def refresh_interval(monkeypatch: pytest.MonkeyPatch) -> int:
    from routstr.core.settings import settings

    monkeypatch.setattr(settings, "models_refresh_interval_seconds", 360)
    return 360


@pytest.mark.asyncio
async def test_failed_fetch_keeps_the_last_good_catalog(
    integration_session: AsyncSession, patched_db_engine: None, refresh_interval: int
) -> None:
    provider_id = await _provider_row(integration_session)
    worker = _worker(provider_id, CATALOG)
    await worker.refresh_models_cache()
    assert _ids(worker) == {"alpha", "beta"}

    # Stale snapshot, so the next refresh goes upstream, and upstream fails.
    snapshot = await integration_session.get(UpstreamModelSnapshot, provider_id)
    assert snapshot is not None
    snapshot.fetched_at -= refresh_interval
    await integration_session.commit()
    worker.fetch_models = AsyncMock(return_value=[])
    await worker.refresh_models_cache()

    worker.fetch_models.assert_awaited_once()
    assert _ids(worker) == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_worker_whose_fetch_fails_serves_a_siblings_catalog(
    integration_session: AsyncSession, patched_db_engine: None, refresh_interval: int
) -> None:
    provider_id = await _provider_row(integration_session)
    healthy = _worker(provider_id, CATALOG)
    await healthy.refresh_models_cache()

    snapshot = await integration_session.get(UpstreamModelSnapshot, provider_id)
    assert snapshot is not None
    snapshot.fetched_at -= refresh_interval  # force the next worker upstream
    await integration_session.commit()

    rate_limited = _worker(provider_id, [])
    await rate_limited.refresh_models_cache()
    assert _ids(rate_limited) == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_fresh_shared_catalog_is_reused_without_fetching(
    integration_session: AsyncSession, patched_db_engine: None, refresh_interval: int
) -> None:
    provider_id = await _provider_row(integration_session)
    first = _worker(provider_id, CATALOG)
    await first.refresh_models_cache()

    others = [_worker(provider_id, CATALOG) for _ in range(5)]
    await asyncio.gather(*(w.refresh_models_cache() for w in others))

    first.fetch_models.assert_awaited_once()
    for worker in others:
        worker.fetch_models.assert_not_awaited()
        assert _ids(worker) == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_snapshot_of_a_different_provider_config_is_ignored(
    integration_session: AsyncSession, patched_db_engine: None, refresh_interval: int
) -> None:
    provider_id = await _provider_row(integration_session)
    await _worker(provider_id, CATALOG, api_key="old").refresh_models_cache()

    # The admin rotated the key: the old catalog must not be served unchecked.
    rotated = _worker(provider_id, [_model("gamma")], api_key="new")
    await rotated.refresh_models_cache()
    rotated.fetch_models.assert_awaited_once()
    assert _ids(rotated) == {"gamma"}


@pytest.mark.asyncio
async def test_concurrent_first_snapshots_do_not_fail_any_worker(
    integration_session: AsyncSession, patched_db_engine: None, refresh_interval: int
) -> None:
    provider_id = await _provider_row(integration_session)
    workers = [_worker(provider_id, CATALOG) for _ in range(6)]
    await asyncio.gather(*(w.refresh_models_cache() for w in workers))
    for worker in workers:
        assert _ids(worker) == {"alpha", "beta"}
    assert await integration_session.get(UpstreamModelSnapshot, provider_id)
