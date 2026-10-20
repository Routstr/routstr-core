"""Disabled providers must not be contacted for model discovery."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import admin_sessions
from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.upstream.generic import GenericUpstreamProvider


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_admin_model_listing_only_fetches_enabled_providers(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
    enabled: bool,
) -> None:
    row = UpstreamProviderRow(
        provider_type="generic",
        base_url="https://api.deepseek.com",
        api_key="invalid-key",
        enabled=enabled,
        provider_fee=1.0,
    )
    integration_session.add(row)
    await integration_session.commit()
    await integration_session.refresh(row)
    assert row.id is not None
    integration_session.add(
        ModelRow(
            id="deepseek-chat",
            upstream_provider_id=row.id,
            name="DeepSeek Chat",
            description="Stored model",
            created=0,
            context_length=128000,
            architecture=json.dumps(
                {
                    "modality": "text",
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "tokenizer": "unknown",
                }
            ),
            pricing=json.dumps({"prompt": 1e-7, "completion": 2e-7}),
            enabled=False,
        )
    )
    await integration_session.commit()

    token = "test-disabled-provider-models"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    fetch = AsyncMock(return_value=[])
    try:
        with patch.object(GenericUpstreamProvider, "fetch_models", new=fetch):
            response = await integration_client.get(
                f"/admin/api/upstream-providers/{row.id}/models",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        body = response.json()
        assert [model["id"] for model in body["db_models"]] == ["deepseek-chat"]
        assert body["remote_models"] == []
        if enabled:
            fetch.assert_awaited_once()
        else:
            fetch.assert_not_awaited()
    finally:
        admin_sessions.pop(token, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cache_refresh_skips_provider_disabled_after_instantiation(
    integration_session: AsyncSession,
    patched_db_engine: None,
) -> None:
    row = UpstreamProviderRow(
        provider_type="generic",
        base_url="https://api.deepseek.com",
        api_key="invalid-key",
        enabled=True,
    )
    integration_session.add(row)
    await integration_session.commit()
    await integration_session.refresh(row)
    provider = GenericUpstreamProvider.from_db_row(row)
    assert provider is not None

    row.enabled = False
    integration_session.add(row)
    await integration_session.commit()

    fetch = AsyncMock(return_value=[])
    with patch.object(provider, "fetch_models", new=fetch):
        await provider.refresh_models_cache()
    fetch.assert_not_awaited()
    assert provider.get_cached_models() == []
