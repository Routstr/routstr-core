"""Regression tests for admin model alias persistence (GitHub issue #639)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import admin_sessions
from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.proxy import reinitialize_upstreams

MODEL_ID = "deepseek/deepseek-chat"
ARCHITECTURE = {
    "modality": "text",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "tokenizer": "unknown",
    "instruct_type": None,
}
PRICING = {
    "prompt": 1e-7,
    "completion": 2e-7,
    "request": 0.0,
    "image": 0.0,
    "web_search": 0.0,
    "internal_reasoning": 0.0,
}


def _admin_headers() -> dict[str, str]:
    token = "test-admin-forwarded-model-id-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


def _model_payload(
    *, forwarded_model_id: str | None, include_alias: bool
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": MODEL_ID,
        "name": "DeepSeek Chat",
        "description": "DeepSeek Chat test model",
        "created": 0,
        "context_length": 128000,
        "architecture": ARCHITECTURE,
        "pricing": PRICING,
        "per_request_limits": None,
        "top_provider": None,
        "canonical_slug": None,
        "alias_ids": [],
        "enabled": True,
    }
    if include_alias:
        payload["forwarded_model_id"] = forwarded_model_id
    return payload


def _model_row(provider_id: int, *, forwarded_model_id: str | None) -> ModelRow:
    return ModelRow(
        id=MODEL_ID,
        upstream_provider_id=provider_id,
        name="DeepSeek Chat",
        description="DeepSeek Chat test model",
        created=0,
        context_length=128000,
        architecture=json.dumps(ARCHITECTURE),
        pricing=json.dumps(PRICING),
        enabled=True,
        forwarded_model_id=forwarded_model_id,
    )


async def _create_provider(
    session: AsyncSession, *, base_url: str
) -> UpstreamProviderRow:
    provider = UpstreamProviderRow(
        provider_type="generic",
        base_url=base_url,
        api_key="test-key",
        provider_fee=1.0,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    assert provider.id is not None
    return provider


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_create_without_forwarded_model_id_preserves_null(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """Saving an unaliased model must not invent a self-alias."""
    provider = await _create_provider(
        integration_session, base_url="https://issue-639-create.example/v1"
    )
    await reinitialize_upstreams()

    with patch("routstr.payment.models.sats_usd_price", return_value=1e-6):
        response = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/models",
            headers=_admin_headers(),
            json=_model_payload(forwarded_model_id=None, include_alias=False),
        )

    assert response.status_code == 200
    assert response.json()["forwarded_model_id"] is None
    row = await integration_session.get(ModelRow, (MODEL_ID, provider.id))
    assert row is not None
    assert row.forwarded_model_id is None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forwarded_model_id", [None, "", "   "], ids=["null", "empty", "blank"]
)
async def test_admin_update_can_clear_forwarded_model_id(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
    forwarded_model_id: str | None,
) -> None:
    """An explicit null or blank value must remove an existing upstream alias."""
    provider = await _create_provider(
        integration_session, base_url="https://issue-639-clear.example/v1"
    )
    assert provider.id is not None
    row = _model_row(provider.id, forwarded_model_id="upstream-deepseek-chat")
    integration_session.add(row)
    await integration_session.commit()
    await reinitialize_upstreams()

    with patch("routstr.payment.models.sats_usd_price", return_value=1e-6):
        response = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/models",
            headers=_admin_headers(),
            json=_model_payload(
                forwarded_model_id=forwarded_model_id, include_alias=True
            ),
        )

    assert response.status_code == 200
    assert response.json()["forwarded_model_id"] is None
    await integration_session.refresh(row)
    assert row.forwarded_model_id is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_update_without_forwarded_model_id_preserves_alias(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """Omitting the alias must not overwrite an existing value."""
    provider = await _create_provider(
        integration_session, base_url="https://issue-639-preserve.example/v1"
    )
    assert provider.id is not None
    row = _model_row(provider.id, forwarded_model_id="upstream-deepseek-chat")
    integration_session.add(row)
    await integration_session.commit()
    await reinitialize_upstreams()

    with patch("routstr.payment.models.sats_usd_price", return_value=1e-6):
        response = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/models",
            headers=_admin_headers(),
            json=_model_payload(forwarded_model_id=None, include_alias=False),
        )

    assert response.status_code == 200
    assert response.json()["forwarded_model_id"] == "upstream-deepseek-chat"
    await integration_session.refresh(row)
    assert row.forwarded_model_id == "upstream-deepseek-chat"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_create_serializes_forwarded_model_id(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """A configured alias must remain visible in the admin response."""
    provider = await _create_provider(
        integration_session, base_url="https://issue-639-serialize.example/v1"
    )
    await reinitialize_upstreams()

    with patch("routstr.payment.models.sats_usd_price", return_value=1e-6):
        response = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/models",
            headers=_admin_headers(),
            json=_model_payload(
                forwarded_model_id="upstream-deepseek-chat", include_alias=True
            ),
        )

    assert response.status_code == 200
    assert response.json()["forwarded_model_id"] == "upstream-deepseek-chat"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_batch_create_preserves_forwarded_model_id(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """Batch creation must persist a distinct upstream alias."""
    provider = await _create_provider(
        integration_session, base_url="https://issue-639-batch-create.example/v1"
    )
    await reinitialize_upstreams()

    response = await integration_client.post(
        f"/admin/api/upstream-providers/{provider.id}/batch-override",
        headers=_admin_headers(),
        json={
            "models": [
                _model_payload(
                    forwarded_model_id="upstream-deepseek-chat", include_alias=True
                )
            ]
        },
    )

    assert response.status_code == 200
    row = await integration_session.get(ModelRow, (MODEL_ID, provider.id))
    assert row is not None
    assert row.forwarded_model_id == "upstream-deepseek-chat"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_batch_update_can_clear_forwarded_model_id(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """Batch overrides must persist an explicit null alias too."""
    provider = await _create_provider(
        integration_session, base_url="https://issue-639-batch-clear.example/v1"
    )
    assert provider.id is not None
    row = _model_row(provider.id, forwarded_model_id="upstream-deepseek-chat")
    integration_session.add(row)
    await integration_session.commit()
    await reinitialize_upstreams()

    with patch("routstr.payment.models.sats_usd_price", return_value=1e-6):
        response = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/batch-override",
            headers=_admin_headers(),
            json={
                "models": [
                    _model_payload(forwarded_model_id=None, include_alias=True)
                ]
            },
        )

    assert response.status_code == 200
    await integration_session.refresh(row)
    assert row.forwarded_model_id is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_batch_update_without_forwarded_model_id_preserves_alias(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """Batch updates must preserve aliases when the field is omitted."""
    provider = await _create_provider(
        integration_session, base_url="https://issue-639-batch-preserve.example/v1"
    )
    assert provider.id is not None
    row = _model_row(provider.id, forwarded_model_id="upstream-deepseek-chat")
    integration_session.add(row)
    await integration_session.commit()
    await reinitialize_upstreams()

    response = await integration_client.post(
        f"/admin/api/upstream-providers/{provider.id}/batch-override",
        headers=_admin_headers(),
        json={
            "models": [
                _model_payload(forwarded_model_id=None, include_alias=False)
            ]
        },
    )

    assert response.status_code == 200
    await integration_session.refresh(row)
    assert row.forwarded_model_id == "upstream-deepseek-chat"
