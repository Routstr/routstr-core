"""Integration tests for model price updates when provider fee schedules change."""

import time
from typing import Any, Generator

import pytest
from httpx import AsyncClient

from routstr.core.admin import admin_sessions

ADMIN_TOKEN = "test-admin-token"


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(autouse=True)
def _inject_admin_session() -> Generator[None, None, None]:
    admin_sessions[ADMIN_TOKEN] = int(time.time()) + 3600
    yield
    admin_sessions.pop(ADMIN_TOKEN, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_price_updates_on_fee_schedule_change(
    integration_client: AsyncClient,
    patched_db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch fetch_models to return empty list to avoid network errors
    # and allow DB models to be used
    from routstr.upstream.base import BaseUpstreamProvider

    async def mock_fetch_models(self: BaseUpstreamProvider) -> list:
        return []

    monkeypatch.setattr(BaseUpstreamProvider, "fetch_models", mock_fetch_models)

    # 1. Create a provider
    provider_resp = await integration_client.post(
        "/admin/api/upstream-providers",
        json={
            "provider_type": "custom",
            "base_url": "https://api.example.com/v1",
            "api_key": "test-key",
            "enabled": True,
            "provider_fee": 1.0,
        },
        headers=_auth_header(),
    )
    provider_id = provider_resp.json()["id"]

    # 2. Add a model to this provider
    model_id = "test-model-price-update"
    await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        json={
            "id": model_id,
            "name": "Test Model",
            "created": int(time.time()),
            "description": "Test",
            "context_length": 4096,
            "architecture": {
                "modality": "text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "gpt2",
                "instruct_type": "none",
            },
            "pricing": {"prompt": 1.0, "completion": 2.0},
            "enabled": True,
        },
        headers=_auth_header(),
    )

    # 3. Check initial price (should be prompt=1.0 * fee=1.0 = 1.0)
    # We use /models endpoint
    resp = await integration_client.get("/models")
    models = resp.json()["data"]
    target = next((m for m in models if m["id"] == model_id), None)
    assert target is not None
    assert target["pricing"]["prompt"] == 1.0

    # 4. Update provider fee schedule to a very high value for the current time
    # We'll use a range that covers the whole day to be safe
    schedules = [
        {"start_time": "00:00", "end_time": "23:59", "provider_fee": 2.5},
    ]
    await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={"schedules": schedules},
        headers=_auth_header(),
    )

    # 5. Check price again - should be updated instantly
    resp = await integration_client.get("/models")
    models = resp.json()["data"]
    target = next((m for m in models if m["id"] == model_id), None)
    assert target is not None
    # 1.0 * 2.5 = 2.5
    assert target["pricing"]["prompt"] == 2.5

    # 6. Delete schedules
    await integration_client.delete(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        headers=_auth_header(),
    )

    # 7. Should revert to default fee (1.0)
    resp = await integration_client.get("/models")
    models = resp.json()["data"]
    target = next((m for m in models if m["id"] == model_id), None)
    assert target is not None
    assert target["pricing"]["prompt"] == 1.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upstream_model_price_updates_on_fee_schedule_change(
    integration_client: AsyncClient,
    patched_db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr.payment.models import Architecture, Model, Pricing
    from routstr.upstream.base import BaseUpstreamProvider

    upstream_model_id = "upstream-model-only"

    # Mock fetch_models to return a model
    async def mock_fetch_models(self: BaseUpstreamProvider) -> list[Model]:
        return [
            Model(
                id=upstream_model_id,
                name="Upstream Model",
                created=int(time.time()),
                description="Test",
                context_length=4096,
                architecture=Architecture(
                    modality="text",
                    input_modalities=["text"],
                    output_modalities=["text"],
                    tokenizer="gpt2",
                    instruct_type="none",
                ),
                pricing=Pricing(prompt=1.0, completion=2.0),
                enabled=True,
            )
        ]

    monkeypatch.setattr(BaseUpstreamProvider, "fetch_models", mock_fetch_models)

    # 1. Create a provider
    provider_resp = await integration_client.post(
        "/admin/api/upstream-providers",
        json={
            "provider_type": "custom",
            "base_url": "https://api.example.com/v1",
            "api_key": "test-key-2",
            "enabled": True,
            "provider_fee": 1.0,
        },
        headers=_auth_header(),
    )
    provider_id = provider_resp.json()["id"]

    # 2. Check initial price (should be prompt=1.0 * fee=1.0 = 1.0)
    resp = await integration_client.get("/models")
    models = resp.json()["data"]
    target = next((m for m in models if m["id"] == upstream_model_id), None)
    assert target is not None
    assert target["pricing"]["prompt"] == 1.0

    # 3. Update provider fee schedule
    schedules = [
        {"start_time": "00:00", "end_time": "23:59", "provider_fee": 3.0},
    ]
    await integration_client.put(
        f"/admin/api/upstream-providers/{provider_id}/fee-schedules",
        json={"schedules": schedules},
        headers=_auth_header(),
    )

    # 4. Check price again - I expect this to FAIL (still 1.0 instead of 3.0)
    resp = await integration_client.get("/models")
    models = resp.json()["data"]
    target = next((m for m in models if m["id"] == upstream_model_id), None)
    assert target is not None
    assert target["pricing"]["prompt"] == 3.0
