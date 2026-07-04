import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import admin_sessions
from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.payment.cost_calculation import CostData, calculate_cost
from routstr.proxy import get_model_instance, reinitialize_upstreams


def _admin_headers() -> dict[str, str]:
    token = "test-admin-cache-pricing-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


def _model_payload(
    provider_id: int,
    *,
    cache_read: float,
    cache_write: float,
) -> dict[str, object]:
    return {
        "id": "custom-cache-model",
        "name": "Custom Cache Model",
        "description": "custom model with explicit cache pricing",
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
            "prompt": 1.4e-7,
            "completion": 2.8e-7,
            "input_cache_read": cache_read,
            "input_cache_write": cache_write,
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
        "enabled": True,
        "forwarded_model_id": "custom-cache-model",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_provider_model_api_persists_cache_pricing_on_create_and_update(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    provider = UpstreamProviderRow(
        provider_type="generic",
        base_url="https://custom-upstream.example/v1",
        api_key="test-key",
        provider_fee=1.0,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    assert provider.id is not None
    await reinitialize_upstreams()

    headers = _admin_headers()
    create_payload = _model_payload(
        provider.id,
        cache_read=2.8e-9,
        cache_write=3.5e-9,
    )

    with patch("routstr.payment.models.sats_usd_price", return_value=1e-6):
        create_response = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/models",
            headers=headers,
            json=create_payload,
        )

    assert create_response.status_code == 200
    create_body = create_response.json()
    assert create_body["pricing"]["input_cache_read"] == pytest.approx(2.8e-9)
    assert create_body["pricing"]["input_cache_write"] == pytest.approx(3.5e-9)

    row = await integration_session.get(ModelRow, ("custom-cache-model", provider.id))
    assert row is not None
    stored_pricing = json.loads(row.pricing)
    assert stored_pricing["input_cache_read"] == pytest.approx(2.8e-9)
    assert stored_pricing["input_cache_write"] == pytest.approx(3.5e-9)

    update_payload = _model_payload(
        provider.id,
        cache_read=1.25e-9,
        cache_write=4.5e-9,
    )

    with patch("routstr.payment.models.sats_usd_price", return_value=1e-6):
        update_response = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/models",
            headers=headers,
            json=update_payload,
        )

    assert update_response.status_code == 200
    update_body = update_response.json()
    assert update_body["pricing"]["input_cache_read"] == pytest.approx(1.25e-9)
    assert update_body["pricing"]["input_cache_write"] == pytest.approx(4.5e-9)

    await integration_session.refresh(row)
    updated_pricing = json.loads(row.pricing)
    assert updated_pricing["input_cache_read"] == pytest.approx(1.25e-9)
    assert updated_pricing["input_cache_write"] == pytest.approx(4.5e-9)

    model = get_model_instance("custom-cache-model")
    assert model is not None
    assert model.sats_pricing is not None
    assert model.sats_pricing.input_cache_read == pytest.approx(0.00125)
    assert model.sats_pricing.input_cache_write == pytest.approx(0.0045)

    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=1e-6):
        cost = await calculate_cost(
            {
                "model": "custom-cache-model",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 800},
                },
            },
            max_cost=1_000_000,
        )

    assert isinstance(cost, CostData)
    assert cost.input_tokens == 200
    assert cost.cache_read_input_tokens == 800
    assert cost.cache_read_msats == 1000
    assert cost.output_msats == 28000
    assert cost.input_msats == 29000
    assert cost.total_msats == 57000


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upstream_response_cost_uses_model_cache_pricing(
    integration_session: AsyncSession,
    patched_db_engine: None,
) -> None:
    """A model's configured cache price must discount upstream cached-token usage."""
    provider = UpstreamProviderRow(
        provider_type="generic",
        base_url="https://cache-priced-upstream.example/v1",
        api_key="test-key",
        provider_fee=1.0,
    )
    integration_session.add(provider)
    await integration_session.commit()
    await integration_session.refresh(provider)
    assert provider.id is not None

    row = ModelRow(
        id="cache-priced-model",
        name="Cache Priced Model",
        description="model seeded with explicit cache pricing",
        created=0,
        context_length=128000,
        architecture=json.dumps(
            {
                "modality": "text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "unknown",
                "instruct_type": None,
            }
        ),
        pricing=json.dumps(
            {
                "prompt": 1.4e-7,
                "completion": 2.8e-7,
                "input_cache_read": 1.25e-9,
                "input_cache_write": 4.5e-9,
            }
        ),
        upstream_provider_id=provider.id,
        enabled=True,
        forwarded_model_id="cache-priced-model",
    )
    integration_session.add(row)
    await integration_session.commit()

    with patch("routstr.payment.models.sats_usd_price", return_value=1e-6):
        await reinitialize_upstreams()

    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=1e-6):
        cost = await calculate_cost(
            {
                "model": "cache-priced-model",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 800},
                },
            },
            max_cost=1_000_000,
        )

    assert isinstance(cost, CostData)
    # prompt 1.4e-7 USD/token -> 0.14 sats/token -> 140 msats/token
    # cache 1.25e-9 USD/token -> 0.00125 sats/token -> 1.25 msats/token
    # completion 2.8e-7 USD/token -> 0.28 sats/token -> 280 msats/token
    assert cost.input_tokens == 200
    assert cost.cache_read_input_tokens == 800
    assert cost.cache_read_msats == 1000
    assert cost.input_msats == 29000
    assert cost.output_msats == 28000
    assert cost.total_msats == 57000
