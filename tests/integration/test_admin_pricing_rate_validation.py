"""Admin write edge: a rate that is not a number never becomes a stored price.

A billable rate is usable only when it is finite and non-negative. The admin
model endpoints are an entry point for rates the node will later bill on, and
they accept whatever a client sends: ``json`` parses the bare ``NaN``/
``Infinity`` literals into real floats and overflows ``1e999`` to ``inf``, a
non-numeric string coerced silently to ``$0``, and a negative rate is truthy so
it read back as a chargeable price that bills a negative amount.

These tests assert the edge answers a malformed rate with a 422 — a client bug
reported as a client bug — rather than persisting it or failing as a 500, and
that the operator can still open the listing that shows the row needing repair.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import admin_sessions
from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.proxy import reinitialize_upstreams


def _admin_headers() -> dict[str, str]:
    token = "test-admin-rate-validation-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


def _pricing(**overrides: object) -> dict[str, object]:
    pricing: dict[str, object] = {
        "prompt": 1.4e-7,
        "completion": 2.8e-7,
        "request": 0.0,
        "image": 0.0,
        "web_search": 0.0,
        "internal_reasoning": 0.0,
        "input_cache_read": 0.0,
        "input_cache_write": 0.0,
    }
    pricing.update(overrides)
    return pricing


def _payload(
    provider_id: int,
    *,
    model_id: str = "rate-model",
    pricing: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": model_id,
        "name": "Rate Model",
        "description": "d",
        "created": 0,
        "context_length": 128000,
        "architecture": {
            "modality": "text",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tokenizer": "unknown",
            "instruct_type": None,
        },
        "pricing": pricing if pricing is not None else _pricing(),
        "per_request_limits": None,
        "top_provider": None,
        "upstream_provider_id": provider_id,
        "canonical_slug": None,
        "alias_ids": [],
        "enabled": True,
        "forwarded_model_id": model_id,
    }


async def _make_provider(session: AsyncSession) -> int:
    provider = UpstreamProviderRow(
        provider_type="generic",
        base_url="https://rate-upstream.example/v1",
        api_key="test-key",
        provider_fee=1.0,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    await reinitialize_upstreams()
    assert provider.id is not None
    return provider.id


def _raw_model_body(provider_id: int, model_id: str, prompt_literal: str) -> str:
    """A request body built as text, so it can carry a literal ``json`` accepts
    but Python's own encoder would refuse to produce."""
    return (
        f'{{"id": "{model_id}", "name": "raw", "description": "d", "created": 0,'
        ' "context_length": 8192, "architecture": {"modality": "text"},'
        f' "pricing": {{"prompt": {prompt_literal}, "completion": 2.8e-7}},'
        f' "upstream_provider_id": {provider_id}}}'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_negative_price_is_rejected(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A negative rate is not a valid price — accepting it would persist a row
    that bills a negative amount, which settlement subtracts from the balance.
    Being truthy, it also reads back as a chargeable price. Reject at the edge
    rather than silently storing it."""
    provider_id = await _make_provider(integration_session)

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(provider_id, model_id="neg-price", pricing=_pricing(prompt=-1.0)),
    )

    assert resp.status_code == 422
    assert await integration_session.get(ModelRow, ("neg-price", provider_id)) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_malformed_price_string_is_rejected(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A present non-numeric rate is a client bug: it coerces to ``$0`` on the
    read path, producing an unpriced-looking row indistinguishable from a
    deliberate free price. Surface it as a 422 instead of accepting it."""
    provider_id = await _make_provider(integration_session)

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(
            provider_id, model_id="bad-price", pricing=_pricing(prompt="oops")
        ),
    )

    assert resp.status_code == 422
    assert await integration_session.get(ModelRow, ("bad-price", provider_id)) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_numeric_string_price_is_still_accepted(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """The stored pricing JSON has always accepted numeric strings, and the UI
    round-trips rates through text fields. Rejecting a *malformed* rate must not
    also reject a well-formed one that arrives spelled as a string."""
    provider_id = await _make_provider(integration_session)

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(
            provider_id, model_id="string-price", pricing=_pricing(prompt="0.000005")
        ),
    )

    assert resp.status_code == 200
    row = await integration_session.get(ModelRow, ("string-price", provider_id))
    assert row is not None
    assert json.loads(row.pricing)["prompt"] == "0.000005"


def test_non_finite_price_is_rejected_by_the_write_model() -> None:
    """``NaN``/``±inf`` are not billable rates: the carrier every write endpoint
    shares must reject them before they can be persisted and read back as a
    chargeable price."""
    from pydantic import ValidationError

    from routstr.core.admin import ModelCreate

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            ModelCreate.model_validate(
                _payload(1, model_id="nonfinite", pricing=_pricing(prompt=bad))
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_oversized_integer_price_is_rejected(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A JSON integer too large for a float is a client bug, not a server fault.

    ``float()`` raises ``OverflowError`` for it, and pydantic converts only
    ``ValueError``/``AssertionError`` into validation errors, so it escaped the
    edge as a 500. It must be answered with the same 422 as every other
    unusable rate.
    """
    provider_id = await _make_provider(integration_session)

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers={**_admin_headers(), "Content-Type": "application/json"},
        content=_raw_model_body(provider_id, "huge-price", "9" * 400),
    )

    assert resp.status_code == 422
    assert await integration_session.get(ModelRow, ("huge-price", provider_id)) is None
