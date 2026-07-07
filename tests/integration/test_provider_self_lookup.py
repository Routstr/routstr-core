"""A live upstream provider resolves its OWN database row by stable identity
(its primary key), not by its mutable/secret ``api_key``.

Today ``from_db_row`` drops ``provider_row.id`` and the two self-referential
paths — PPQ.AI's insufficient-balance self-disable and the base
``refresh_models_cache`` — re-find their own row with
``WHERE base_url == self.base_url AND api_key == self.api_key``. That uses a
rotatable secret as a self-handle: the moment the row's key changes underneath a
live object (a rotation racing an in-flight request), the object can no longer
find itself. These tests pin the invariant that a provider looks itself up by
identity, so the lookup survives a key change (and, later, key encryption).
"""

from unittest.mock import AsyncMock, patch

import pytest

from routstr.core.db import UpstreamProviderRow
from routstr.upstream.ppqai import PPQAIUpstreamProvider


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_object_carries_its_persistent_identity(
    integration_session: object,
    patched_db_engine: None,
) -> None:
    """``from_db_row`` gives the in-memory object its row's identity (``db_id``)."""
    row = UpstreamProviderRow(
        provider_type="ppqai",
        base_url="https://api.ppq.ai",
        api_key="sk-original",
        enabled=True,
        provider_fee=1.0,
    )
    integration_session.add(row)  # type: ignore[attr-defined]
    await integration_session.commit()  # type: ignore[attr-defined]
    await integration_session.refresh(row)  # type: ignore[attr-defined]

    provider = PPQAIUpstreamProvider.from_db_row(row)
    assert provider is not None

    assert provider.db_id == row.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_self_disable_targets_own_row_after_key_rotation(
    integration_session: object,
    patched_db_engine: None,
) -> None:
    """PPQ.AI self-disable must disable *its* row even after the key rotated.

    RED (current): the object holds the pre-rotation key, so the
    ``(base_url, api_key)`` lookup misses the row → the provider is never
    disabled. GREEN: lookup by ``id`` finds it and disables it.
    """
    row = UpstreamProviderRow(
        provider_type="ppqai",
        base_url="https://api.ppq.ai",
        api_key="sk-original",
        enabled=True,
        provider_fee=1.0,
    )
    integration_session.add(row)  # type: ignore[attr-defined]
    await integration_session.commit()  # type: ignore[attr-defined]
    await integration_session.refresh(row)  # type: ignore[attr-defined]

    provider = PPQAIUpstreamProvider.from_db_row(row)  # captures sk-original
    assert provider is not None

    # Key is rotated in the DB while `provider` is still live.
    row.api_key = "sk-rotated"
    integration_session.add(row)  # type: ignore[attr-defined]
    await integration_session.commit()  # type: ignore[attr-defined]

    with patch("routstr.proxy.reinitialize_upstreams", new=AsyncMock()):
        await provider.on_upstream_error_redirect(402, "Insufficient balance")

    await integration_session.refresh(row)  # type: ignore[attr-defined]
    assert row.enabled is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refresh_models_cache_finds_own_row_after_key_rotation(
    integration_session: object,
    patched_db_engine: None,
) -> None:
    """``refresh_models_cache`` must resolve its own row after a key rotation.

    ``refresh_models_cache`` swallows every exception (it only logs), so the
    observable proof it found its row is that it reaches ``list_models`` — which
    is called with the row's ``id`` only *after* the row is resolved. RED
    (current): the stale-key ``(base_url, api_key)`` lookup returns nothing, the
    method raises ``404`` internally and returns before ``list_models`` is ever
    called. GREEN: lookup by ``id`` finds the row and ``list_models`` runs for
    that ``id``.
    """
    row = UpstreamProviderRow(
        provider_type="ppqai",
        base_url="https://api.ppq.ai",
        api_key="sk-original",
        enabled=True,
        provider_fee=1.0,
    )
    integration_session.add(row)  # type: ignore[attr-defined]
    await integration_session.commit()  # type: ignore[attr-defined]
    await integration_session.refresh(row)  # type: ignore[attr-defined]
    row_id = row.id

    provider = PPQAIUpstreamProvider.from_db_row(row)  # captures sk-original
    assert provider is not None

    row.api_key = "sk-rotated"
    integration_session.add(row)  # type: ignore[attr-defined]
    await integration_session.commit()  # type: ignore[attr-defined]

    list_models_mock = AsyncMock(return_value=[])
    with (
        patch.object(provider, "fetch_models", new=AsyncMock(return_value=[])),
        patch("routstr.upstream.base.list_models", new=list_models_mock),
    ):
        await provider.refresh_models_cache()

    list_models_mock.assert_awaited_once()
    assert list_models_mock.await_args is not None
    assert list_models_mock.await_args.kwargs["upstream_id"] == row_id
