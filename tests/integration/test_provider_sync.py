"""Provider changes reach every worker, not only the one that handled them.

Each worker builds its own provider objects. An admin change used to rebuild
them only in the worker that served the request, so the others kept routing
with a rotated key or a deleted provider until they restarted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr import proxy
from routstr.core.db import UpstreamProviderRow
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


@pytest.fixture(autouse=True)
def isolated_proxy_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "_upstreams", [])
    monkeypatch.setattr(proxy, "_upstreams_config_version", None)
    monkeypatch.setattr(proxy, "_provider_map", {})
    monkeypatch.setattr(proxy, "_unique_models", {})
    monkeypatch.setattr(
        BaseUpstreamProvider, "fetch_models", AsyncMock(return_value=[_model("m")])
    )


async def _add_provider(session: AsyncSession) -> UpstreamProviderRow:
    row = UpstreamProviderRow(
        provider_type="custom", base_url="https://sync.example/v1", api_key="old-key"
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _served_key() -> str | None:
    candidates = proxy.get_candidates("m")
    return candidates[0][1].api_key if candidates else None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unchanged_providers_are_not_rebuilt(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    await _add_provider(integration_session)
    await proxy.initialize_upstreams()

    with patch.object(proxy, "init_upstreams", AsyncMock()) as init:
        assert await proxy.sync_upstreams_with_database() is False
    init.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_key_rotated_elsewhere_reaches_this_worker(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    row = await _add_provider(integration_session)
    await proxy.initialize_upstreams()
    assert _served_key() == "old-key"

    # Written by another worker: this one's providers are not rebuilt directly.
    row.api_key = "new-key"
    integration_session.add(row)
    await integration_session.commit()

    assert await proxy.sync_upstreams_with_database() is True
    assert [u.api_key for u in proxy.get_upstreams()] == ["new-key"]
    assert _served_key() == "new-key"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "disable"])
async def test_a_provider_removed_elsewhere_stops_serving_here(
    integration_session: AsyncSession, patched_db_engine: None, change: str
) -> None:
    row = await _add_provider(integration_session)
    # An empty table is re-seeded from the environment; keep one row around.
    integration_session.add(
        UpstreamProviderRow(
            provider_type="custom",
            base_url="https://other.example/v1",
            api_key="k",
            enabled=False,
        )
    )
    await integration_session.commit()
    await proxy.initialize_upstreams()
    assert _served_key() == "old-key"

    if change == "delete":
        await integration_session.delete(row)
    else:
        row.enabled = False
        integration_session.add(row)
    await integration_session.commit()

    assert await proxy.sync_upstreams_with_database() is True
    assert proxy.get_upstreams() == []
    assert proxy.get_candidates("m") is None
