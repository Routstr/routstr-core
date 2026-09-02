"""Characterization tests for the model-serialisation pipeline ``_row_to_model`` runs.

These pin what the three public surfaces that reach it serve today: ``GET
/v1/models``, the admin single-model read-back, and the admin provider model
listing.  Covered are the plain model, litellm cache backfill, preservation of
explicit cache rates, the request-price floor, the provider-fee flag against
recomputed max costs, survival of a failed sats conversion, the full serialised
dict field for field, and agreement between the two admin views.

Nothing here asserts on the deterministic USD half in isolation, so the pins hold
whether or not it is split out from the live BTC-rate conversion.
"""

from __future__ import annotations

import json
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
    token = "test-serialisation-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


# -- helpers -------------------------------------------------------------------

_SEEDED_MODEL_ID = "ser-test-model"


async def _seed_provider(session: AsyncSession, fee: float = 1.0) -> int:
    """Insert a provider, refresh the upstream map, and return its primary key."""
    provider = UpstreamProviderRow(
        provider_type="generic",
        base_url="https://serialisation-test.example/v1",
        api_key="test-key",
        provider_fee=fee,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    await reinitialize_upstreams()
    assert provider.id is not None
    return provider.id


async def _seed_model(
    session: AsyncSession,
    provider_id: int,
    *,
    model_id: str = _SEEDED_MODEL_ID,
    prompt: float = 1.0e-7,
    completion: float = 2.0e-7,
    cache_read: float = 0.0,
    cache_write: float = 0.0,
    request_price: float = 0.0,
    enabled: bool = True,
) -> ModelRow:
    row = ModelRow(
        id=model_id,
        name=f"SerTest {model_id}",
        description="characterization model",
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
                "prompt": prompt,
                "completion": completion,
                "input_cache_read": cache_read,
                "input_cache_write": cache_write,
                "request": request_price,
                "image": 0.0,
                "web_search": 0.0,
                "internal_reasoning": 0.0,
            }
        ),
        upstream_provider_id=provider_id,
        enabled=enabled,
        forwarded_model_id=model_id,
    )
    session.add(row)
    await session.commit()
    return row


async def _raw_via_admin(client: AsyncClient, provider_id: int, model_id: str) -> dict:
    """Return the raw (``apply_provider_fee=False``) model dict from admin read-back."""
    r = await client.get(
        f"/admin/api/upstream-providers/{provider_id}/models/{model_id}",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    return r.json()


async def _served_via_public(client: AsyncClient, model_id: str) -> dict | None:
    """Return the served model dict from /v1/models, or None if absent."""
    r = await client.get("/v1/models")
    assert r.status_code == 200
    return {m["id"]: m for m in r.json()["data"]}.get(model_id)


# -- test 1: plain model -------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plain_model_serialisation(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """A model with prompt + completion prices has the expected serialised shape."""
    provider_id = await _seed_provider(integration_session)
    await _seed_model(integration_session, provider_id)
    await reinitialize_upstreams()

    # Admin read-back (raw, no fee)
    body = await _raw_via_admin(
        integration_client,
        provider_id,
        _SEEDED_MODEL_ID,
    )
    assert body["id"] == _SEEDED_MODEL_ID
    assert body["pricing"]["prompt"] == pytest.approx(1.0e-7)
    assert body["pricing"]["completion"] == pytest.approx(2.0e-7)
    assert body["sats_pricing"] is not None
    # With sats_usd_price = 0.0005 (the fixture)
    assert body["sats_pricing"]["prompt"] == pytest.approx(1.0e-7 / 0.0005)
    assert body["sats_pricing"]["completion"] == pytest.approx(2.0e-7 / 0.0005)

    # Public /v1/models (fee applied)
    s = await _served_via_public(integration_client, _SEEDED_MODEL_ID)
    assert s is not None, f"{_SEEDED_MODEL_ID} not found in /v1/models"
    # fee=1.0 so values match raw
    assert s["pricing"]["prompt"] == pytest.approx(1.0e-7)
    assert s["pricing"]["completion"] == pytest.approx(2.0e-7)


# -- test 2: no cache rates (litellm backfill) ---------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_without_cache_rates_gets_litellm_backfill(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """A well-known model without cache rates gets them from litellm's cost map."""
    provider_id = await _seed_provider(integration_session)
    # Use a real litellm-known id so backfill_cache_pricing can find it.
    await _seed_model(
        integration_session,
        provider_id,
        model_id="gpt-4o",
        prompt=2.5e-6,
        completion=1.0e-5,
        cache_read=0.0,
        cache_write=0.0,
    )
    await reinitialize_upstreams()

    # Admin read-back (raw): cache_read should be present after backfill.
    # (cache_write may not be in litellm's map for every model.)
    body = await _raw_via_admin(
        integration_client,
        provider_id,
        "gpt-4o",
    )
    assert body["pricing"]["input_cache_read"] > 0.0, (
        "backfill_cache_pricing should have filled input_cache_read from litellm"
    )


# -- test 3: cache rates already present (not overwritten) ---------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_existing_cache_rates_not_overwritten(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """A model with explicit cache rates must keep them; the backfill is a no-op."""
    provider_id = await _seed_provider(integration_session)
    await _seed_model(
        integration_session,
        provider_id,
        prompt=1.0e-7,
        completion=2.0e-7,
        cache_read=9.99e-9,
        cache_write=8.88e-9,
    )
    await reinitialize_upstreams()

    body = await _raw_via_admin(
        integration_client,
        provider_id,
        _SEEDED_MODEL_ID,
    )
    assert body["pricing"]["input_cache_read"] == pytest.approx(9.99e-9)
    assert body["pricing"]["input_cache_write"] == pytest.approx(8.88e-9)


# -- test 4: request price floor -----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_with_request_price(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """A model with a request price floor carries it through to the served model."""
    provider_id = await _seed_provider(integration_session)
    await _seed_model(integration_session, provider_id, request_price=0.01)
    await reinitialize_upstreams()

    body = await _raw_via_admin(
        integration_client,
        provider_id,
        _SEEDED_MODEL_ID,
    )
    assert body["pricing"]["request"] == pytest.approx(0.01)


# -- test 5: provider_fee=True vs False, max costs recomputed ------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fee_flag_changes_pricing_but_max_costs_are_recomputed(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """With provider_fee=1.5, fee-adjusted pricing is 1.5× raw, but max costs are NOT."""
    provider_id = await _seed_provider(integration_session, fee=1.5)
    await _seed_model(integration_session, provider_id)
    await reinitialize_upstreams()

    # Admin read-back: raw, no fee.
    body = await _raw_via_admin(
        integration_client,
        provider_id,
        _SEEDED_MODEL_ID,
    )
    assert body["pricing"]["prompt"] == pytest.approx(1.0e-7)

    # Public /v1/models: fee applied.
    s = await _served_via_public(integration_client, _SEEDED_MODEL_ID)
    assert s is not None
    assert s["pricing"]["prompt"] == pytest.approx(1.0e-7 * 1.5)

    # max_prompt_cost must NOT just be multiplied by 1.5 — it is recomputed from
    # the fee-inflated per-token rates and context_length.
    cl = 128_000
    expected_max_prompt = cl * 1.0e-7 * 1.5
    assert s["pricing"]["max_prompt_cost"] == pytest.approx(expected_max_prompt)


# -- test 6: sats conversion failure keeps the model alive ---------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_survives_sats_conversion_failure(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """When the BTC feed fails, the model still returns — with no sats_pricing."""
    provider_id = await _seed_provider(integration_session)
    await _seed_model(integration_session, provider_id)

    # Make sats_usd_price raise so _update_model_sats_pricing swallows it.
    # The admin read-back must happen inside the patch block.
    with patch(
        "routstr.payment.models.sats_usd_price",
        side_effect=RuntimeError("BTC feed down"),
    ):
        await reinitialize_upstreams()
        body = await _raw_via_admin(
            integration_client,
            provider_id,
            _SEEDED_MODEL_ID,
        )
    assert body["id"] == _SEEDED_MODEL_ID
    assert body["sats_pricing"] is None, (
        "sats conversion failure must not crash — the model returns with no sats_pricing"
    )


# -- test 7: the whole serialised dict -----------------------------------------

# The sats figures are the USD ones divided by the pinned 0.0005 rate; written
# as the division so the expectation carries the same float error the code does.
_SATS = 0.0005


def _expected_serialised_model(provider_id: int) -> dict:
    """Every field the raw admin read-back produces for the seeded model.

    Note the max-cost asymmetry: with fees off the USD max costs stay at zero
    while their sats counterparts are computed.  That is what the code does
    today, and pinning it is the point.
    """
    return {
        "alias_ids": None,
        "architecture": {
            "input_modalities": ["text"],
            "instruct_type": None,
            "modality": "text",
            "output_modalities": ["text"],
            "tokenizer": "unknown",
        },
        "canonical_slug": None,
        "context_length": 128000,
        "created": 0,
        "description": "characterization model",
        "enabled": True,
        "forwarded_model_id": _SEEDED_MODEL_ID,
        "id": _SEEDED_MODEL_ID,
        "name": f"SerTest {_SEEDED_MODEL_ID}",
        "per_request_limits": None,
        "pricing": {
            "completion": 2.0e-7,
            "image": 0.0,
            "input_cache_read": 0.0,
            "input_cache_write": 0.0,
            "internal_reasoning": 0.0,
            "max_completion_cost": 0.0,
            "max_cost": 0.0,
            "max_prompt_cost": 0.0,
            "prompt": 1.0e-7,
            "request": 0.01,
            "web_search": 0.0,
        },
        "sats_pricing": {
            "completion": 2.0e-7 / _SATS,
            "image": 0.0,
            "input_cache_read": 0.0,
            "input_cache_write": 0.0,
            "internal_reasoning": 0.0,
            "max_completion_cost": 0.0,
            "max_cost": 0.001,
            "max_prompt_cost": 0.0,
            "prompt": 1.0e-7 / _SATS,
            "request": 0.01 / _SATS,
            "web_search": 0.0,
        },
        "top_provider": None,
        "upstream_provider_id": provider_id,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_serialised_model_is_unchanged(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """Pin every field of the serialised model, not just the interesting ones.

    The tests above pin values.  A refactor that dropped a field outright
    would satisfy all of them and fail only here.
    """
    provider_id = await _seed_provider(integration_session)
    await _seed_model(integration_session, provider_id, request_price=0.01)
    await reinitialize_upstreams()

    body = await _raw_via_admin(
        integration_client,
        provider_id,
        _SEEDED_MODEL_ID,
    )
    assert body == _expected_serialised_model(provider_id)


# -- test 8: the provider model listing ----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_listing_matches_single_model_read_back(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
) -> None:
    """The listing is a third entry into the same builder — it must agree."""
    provider_id = await _seed_provider(integration_session)
    await _seed_model(integration_session, provider_id, request_price=0.01)
    await reinitialize_upstreams()

    single = await _raw_via_admin(
        integration_client,
        provider_id,
        _SEEDED_MODEL_ID,
    )

    r = await integration_client.get(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
    )
    assert r.status_code == 200
    listed = {m["id"]: m for m in r.json()["db_models"]}
    assert _SEEDED_MODEL_ID in listed, "seeded model missing from the provider listing"
    assert listed[_SEEDED_MODEL_ID] == single
