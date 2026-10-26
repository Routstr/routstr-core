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
async def test_failed_fetch_uses_newer_worker_local_catalog(
    integration_session: AsyncSession,
    patched_db_engine: None,
    refresh_interval: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_id = await _provider_row(integration_session)
    await _worker(provider_id, [_model("old-shared")]).refresh_models_cache()

    snapshot = await integration_session.get(UpstreamModelSnapshot, provider_id)
    assert snapshot is not None
    snapshot.fetched_at -= refresh_interval
    await integration_session.commit()

    worker = _worker(provider_id, [_model("new-local")])
    monkeypatch.setattr(
        "routstr.upstream.model_snapshots.save_snapshot", AsyncMock(return_value=None)
    )
    models = await worker._shared_upstream_models(provider_id)
    assert [model.id for model in models] == ["new-local"]

    worker.fetch_models = AsyncMock(return_value=[])
    models = await worker._shared_upstream_models(provider_id)

    assert [model.id for model in models] == ["new-local"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_result", ["fresh", "empty", "error", "local"])
async def test_anthropic_snapshot_restores_native_model_names(
    integration_session: AsyncSession,
    patched_db_engine: None,
    refresh_interval: int,
    monkeypatch: pytest.MonkeyPatch,
    refresh_result: str,
) -> None:
    from routstr.upstream.anthropic import AnthropicUpstreamProvider

    provider_id = await _provider_row(integration_session)
    first = AnthropicUpstreamProvider(api_key="k")
    monkeypatch.setattr(
        first, "_fetch_native_model_ids", AsyncMock(return_value={"claude-haiku-5-5"})
    )
    monkeypatch.setattr(
        "routstr.upstream.anthropic.async_fetch_openrouter_models",
        AsyncMock(return_value=[_model("claude-haiku-5.5").dict()]),
    )
    await first._shared_upstream_models(provider_id)
    snapshot = await integration_session.get(UpstreamModelSnapshot, provider_id)
    assert snapshot is not None
    if refresh_result != "fresh":
        snapshot.fetched_at -= refresh_interval
        await integration_session.commit()
    worker = AnthropicUpstreamProvider(api_key="k")
    if refresh_result == "local":
        worker = first
        worker._native_model_names = {}
        await integration_session.delete(snapshot)
        await integration_session.commit()
    fetch = AsyncMock(return_value=[])
    if refresh_result == "error":
        fetch.side_effect = RuntimeError("native discovery unavailable")
    monkeypatch.setattr(worker, "fetch_models", fetch)

    models = await worker._shared_upstream_models(provider_id)

    assert [m.id for m in models] == ["claude-haiku-5.5"]
    assert worker.transform_model_name(models[0].id) == "claude-haiku-5-5"
    if refresh_result == "fresh":
        fetch.assert_not_awaited()
    else:
        fetch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch_fails", [False, True])
@pytest.mark.parametrize("same_worker", [False, True])
async def test_together_snapshot_invalidated_by_image_price_settings(
    integration_session: AsyncSession,
    patched_db_engine: None,
    refresh_interval: int,
    monkeypatch: pytest.MonkeyPatch,
    fetch_fails: bool,
    same_worker: bool,
) -> None:
    import json

    from routstr.upstream.together import TogetherUpstreamProvider

    model_id = "operator/custom-image"
    row = UpstreamProviderRow(
        provider_type="together",
        base_url=TogetherUpstreamProvider.default_base_url,
        api_key="test-snapshot-credential",
        provider_settings=json.dumps({"image_prices": {model_id: 0.01}}),
    )
    integration_session.add(row)
    await integration_session.commit()
    await integration_session.refresh(row)
    assert row.id is not None
    first = TogetherUpstreamProvider.from_db_row(row)
    assert first is not None
    monkeypatch.setattr(
        first,
        "_fetch_catalog",
        AsyncMock(return_value=[{"id": model_id, "type": "image"}]),
    )
    old = await first._shared_upstream_models(row.id)
    assert old[0].pricing.image_output == 0.01
    row.provider_settings = json.dumps({"image_prices": {model_id: 0.02}})
    await integration_session.commit()
    updated = first if same_worker else TogetherUpstreamProvider.from_db_row(row)
    assert updated is not None
    if same_worker:
        updated.image_prices = {model_id: 0.02}
    fetch = AsyncMock(
        return_value=[] if fetch_fails else [{"id": model_id, "type": "image"}]
    )
    monkeypatch.setattr(updated, "_fetch_catalog", fetch)

    models = await updated._shared_upstream_models(row.id)

    fetch.assert_awaited_once()
    if fetch_fails:
        assert models == []  # A failed refresh must not revive the old price.
    else:
        assert models[0].pricing.image_output == 0.02
    await integration_session.refresh(row)
    snapshot = await integration_session.get(UpstreamModelSnapshot, row.id)
    assert snapshot is not None
    assert row.api_key not in snapshot.models_json
    assert row.api_key not in snapshot.config_hash
