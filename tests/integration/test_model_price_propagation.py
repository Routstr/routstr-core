"""Cover that an admin price write reaches the served catalogue (GET /v1/models).

A price edit changes the served price; the served price is fee-adjusted while the
admin read-back is raw; a disabled model leaves the catalogue but keeps its row.

Each test reaches the served model by id rather than iterating ``data["data"]``,
so an empty catalogue fails these tests instead of skipping them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import admin_sessions
from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.proxy import reinitialize_upstreams


# The conftest patches ``routstr.payment.price.sats_usd_price``, but
# ``models.py`` imports it as ``from .price import sats_usd_price`` — a
# local binding the conftest-level patch cannot reach.  Pin it here so
# every test that goes through ``_row_to_model`` gets a real sats price.
@pytest.fixture(autouse=True)
def _pin_sats_usd() -> Iterator[None]:
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        yield


def _admin_headers() -> dict[str, str]:
    token = "test-propagation-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


def _model_payload(prompt: float, provider_id: int, enabled: bool = True) -> dict:
    return {
        "id": "propagation-test-model",
        "name": "Propagation Test Model",
        "description": "model used to verify price propagation",
        "created": 0,
        "context_length": 128000,
        "architecture": {
            "modality": "text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tokenizer": "unknown",
            "instruct_type": None,
        },
        "pricing": {
            "prompt": prompt,
            "completion": prompt * 2,
            "input_cache_read": 0.0,
            "input_cache_write": 0.0,
            "request": 0.0,
            "image": 0.0,
            "web_search": 0.0,
            "internal_reasoning": 0.0,
        },
        "per_request_limits": None,
        "top_provider": None,
        "upstream_provider_id": provider_id,
        "canonical_slug": None,
        "alias_ids": [],
        "enabled": enabled,
        "forwarded_model_id": "propagation-test-model",
    }


async def _seed_provider(session: AsyncSession, *, fee: float = 1.0) -> int:
    """Insert a provider, refresh the upstream map, and return its primary key."""
    provider = UpstreamProviderRow(
        provider_type="generic",
        base_url="https://propagation-test.example/v1",
        api_key="test-key",
        provider_fee=fee,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    await reinitialize_upstreams()
    assert provider.id is not None
    return provider.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_price_edit_propagates_to_served_catalogue(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """A price edit through the admin API must change the served /v1/models price."""

    provider_id = await _seed_provider(integration_session)

    headers = _admin_headers()
    r = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_model_payload(prompt=1.0e-7, provider_id=provider_id),
    )
    assert r.status_code == 200

    # -- record the served price before edit -----------------------------------
    public = await integration_client.get("/v1/models")
    assert public.status_code == 200
    public_data = public.json()
    assert len(public_data["data"]) > 0, "catalogue must not be empty"
    served_before = {
        m["id"]: m.get("pricing", {}).get("prompt") for m in public_data["data"]
    }
    assert "propagation-test-model" in served_before
    before = served_before["propagation-test-model"]

    # -- edit the price and re-check -------------------------------------------
    r = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_model_payload(prompt=5.0e-7, provider_id=provider_id),
    )
    assert r.status_code == 200

    public = await integration_client.get("/v1/models")
    assert public.status_code == 200
    served_after = {
        m["id"]: m.get("pricing", {}).get("prompt") for m in public.json()["data"]
    }
    after = served_after["propagation-test-model"]

    assert before != after, "served price did not change after admin edit"
    # With provider_fee=1.0 the served price equals the stored raw price.
    assert after == pytest.approx(5.0e-7)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_readback_is_raw_served_is_fee_adjusted(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """Admin read-back returns the raw price; /v1/models returns the fee-adjusted one."""

    provider_id = await _seed_provider(integration_session, fee=1.05)

    headers = _admin_headers()
    model_payload = _model_payload(prompt=1.0e-7, provider_id=provider_id)
    r = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=model_payload,
    )
    assert r.status_code == 200

    # Admin read-back: apply_provider_fee=False
    admin_r = await integration_client.get(
        f"/admin/api/upstream-providers/{provider_id}/models/propagation-test-model",
        headers=headers,
    )
    assert admin_r.status_code == 200
    admin_body = admin_r.json()
    raw_prompt = admin_body["pricing"]["prompt"]
    assert raw_prompt == pytest.approx(1.0e-7)

    # Public /v1/models: fee-adjusted
    public = await integration_client.get("/v1/models")
    assert public.status_code == 200
    served = {
        m["id"]: m.get("pricing", {}).get("prompt") for m in public.json()["data"]
    }
    assert served["propagation-test-model"] == pytest.approx(1.0e-7 * 1.05)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disabled_model_not_served(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """A disabled model must be absent from /v1/models but still present in the DB."""

    provider_id = await _seed_provider(integration_session)

    headers = _admin_headers()
    r = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_model_payload(prompt=1.0e-7, provider_id=provider_id, enabled=True),
    )
    assert r.status_code == 200

    # Confirm it appears in the public catalogue.
    public = await integration_client.get("/v1/models")
    served_ids = {m["id"] for m in public.json()["data"]}
    assert "propagation-test-model" in served_ids

    # -- disable via upsert ----------------------------------------------------
    r = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_model_payload(prompt=1.0e-7, provider_id=provider_id, enabled=False),
    )
    assert r.status_code == 200

    # Public catalogue must no longer list it.
    public = await integration_client.get("/v1/models")
    served_ids = {m["id"] for m in public.json()["data"]}
    assert "propagation-test-model" not in served_ids

    # DB row must still exist.
    row = await integration_session.get(
        ModelRow, ("propagation-test-model", provider_id)
    )
    assert row is not None
    assert row.enabled is False
