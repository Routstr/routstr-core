"""Admin persist-path provenance: how ``pricing_source`` is set on write.

Rules under test (price-edit-only ``manual`` semantics):
- a hand-created model with a real price and no provenance is ``manual``;
- a hand-created model priced at zero with no provenance is ``unresolved``
  (a free import can't be told from an unpriced one), imported disabled;
- a create that carries provenance adopts it (a "save as fetched");
- editing a price flips the row to ``manual``;
- saving an *unchanged* price preserves the resolved source — even when the
  read-back view carries litellm cache rates the stored JSON lacked (the
  false-flip regression).

Money-safety (zero-price handling): a row priced at zero whose source is not
``manual`` is *force-disabled* on write rather than rejected, so it can never
bill at nothing. A ``manual`` price — including an operator editing a price
down to zero — is a deliberate declaration and its ``enabled`` state is left
exactly as the write requests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import admin_sessions
from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.payment.models import _row_to_model
from routstr.proxy import reinitialize_upstreams


def _admin_headers() -> dict[str, str]:
    token = "test-admin-provenance-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


def _payload(
    provider_id: int,
    *,
    model_id: str = "prov-model",
    prompt: float = 1.4e-7,
    completion: float = 2.8e-7,
    enabled: bool = True,
    pricing: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "id": model_id,
        "name": "Prov Model",
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
        "pricing": pricing
        if pricing is not None
        else {
            "prompt": prompt,
            "completion": completion,
            "request": 0.0,
            "image": 0.0,
            "web_search": 0.0,
            "internal_reasoning": 0.0,
            "input_cache_read": 0.0,
            "input_cache_write": 0.0,
        },
        "per_request_limits": None,
        "top_provider": None,
        "upstream_provider_id": provider_id,
        "canonical_slug": None,
        "alias_ids": [],
        "enabled": enabled,
        "forwarded_model_id": model_id,
    }
    body.update(extra)
    return body


async def _make_provider(session: AsyncSession) -> int:
    provider = UpstreamProviderRow(
        provider_type="generic",
        base_url="https://prov-upstream.example/v1",
        api_key="test-key",
        provider_fee=1.0,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    await reinitialize_upstreams()
    assert provider.id is not None
    return provider.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hand_created_model_is_manual(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _make_provider(integration_session)
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(provider_id, model_id="hand-made"),
    )
    assert resp.status_code == 200
    row = await integration_session.get(ModelRow, ("hand-made", provider_id))
    assert row is not None
    assert row.pricing_source == "manual"
    assert row.pricing_checked_at is not None
    assert row.pricing_source_version is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_adopts_payload_provenance(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _make_provider(integration_session)
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(
            provider_id,
            model_id="as-fetched",
            pricing_source="litellm",
            pricing_checked_at=1700000000,
            pricing_source_version="1.83.0",
        ),
    )
    assert resp.status_code == 200
    row = await integration_session.get(ModelRow, ("as-fetched", provider_id))
    assert row is not None
    assert row.pricing_source == "litellm"
    assert row.pricing_checked_at == 1700000000
    assert row.pricing_source_version == "1.83.0"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_price_edit_flips_to_manual(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _make_provider(integration_session)
    headers = _admin_headers()
    # Seed a litellm-sourced row.
    await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_payload(provider_id, model_id="edit-me", pricing_source="litellm"),
    )
    # Edit the prompt price → must become manual.
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_payload(
            provider_id, model_id="edit-me", prompt=9.9e-7, pricing_source="litellm"
        ),
    )
    assert resp.status_code == 200
    row = await integration_session.get(ModelRow, ("edit-me", provider_id))
    assert row is not None
    assert row.pricing_source == "manual"
    assert row.pricing_source_version is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unchanged_price_preserves_source_despite_cache_backfill(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """The false-flip regression: a litellm-known model stored without cache
    rates reads back *with* them (backfilled on read). Re-saving that fee-free
    view unchanged must NOT flip to manual — the comparison is against the same
    backfilled view the UI was shown, not the raw stored JSON."""
    provider_id = await _make_provider(integration_session)
    # Seed deepseek-chat (litellm-known) with NO cache rates in stored pricing.
    row = ModelRow(
        id="deepseek-chat",
        name="DeepSeek",
        description="d",
        created=0,
        context_length=131072,
        architecture=json.dumps(
            {
                "modality": "text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "unknown",
                "instruct_type": None,
            }
        ),
        pricing=json.dumps({"prompt": 2.8e-7, "completion": 4.2e-7}),
        upstream_provider_id=provider_id,
        enabled=True,
        forwarded_model_id="deepseek-chat",
        pricing_source="litellm",
        pricing_checked_at=1700000000,
        pricing_source_version="1.83.0",
    )
    integration_session.add(row)
    await integration_session.commit()

    # The fee-free view the admin UI is served (cache rates now backfilled).
    view = _row_to_model(row, apply_provider_fee=False)
    assert view.pricing.input_cache_read > 0  # proves backfill happened

    # Re-save that exact view, provenance omitted → must preserve litellm.
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(
            provider_id,
            model_id="deepseek-chat",
            pricing=view.pricing.dict(),
        ),
    )
    assert resp.status_code == 200
    await integration_session.refresh(row)
    assert row.pricing_source == "litellm"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_override_create_and_price_edit(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _make_provider(integration_session)
    headers = _admin_headers()
    # Batch create carrying provenance → adopted.
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/batch-override",
        headers=headers,
        json={
            "models": [
                _payload(
                    provider_id, model_id="batch-a", pricing_source="openrouter"
                )
            ]
        },
    )
    assert resp.status_code == 200
    row = await integration_session.get(ModelRow, ("batch-a", provider_id))
    assert row is not None and row.pricing_source == "openrouter"

    # Batch update editing the price → manual.
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/batch-override",
        headers=headers,
        json={
            "models": [
                _payload(
                    provider_id,
                    model_id="batch-a",
                    prompt=5e-7,
                    pricing_source="openrouter",
                )
            ]
        },
    )
    assert resp.status_code == 200
    await integration_session.refresh(row)
    assert row.pricing_source == "manual"


# ---------------------------------------------------------------------------
# money-safety — a zero-price non-manual row is force-disabled, not billed
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_zero_price_without_provenance_is_unresolved_and_disabled(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """The create-bypass fix: a client that omits provenance (today's UI) and
    posts a zero price gets ``unresolved`` + disabled, not a laundered
    ``manual`` $0 enabled at nothing."""
    provider_id = await _make_provider(integration_session)
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(
            provider_id,
            model_id="silent-free",
            prompt=0.0,
            completion=0.0,
            enabled=True,
        ),
    )
    assert resp.status_code == 200
    row = await integration_session.get(ModelRow, ("silent-free", provider_id))
    assert row is not None
    assert row.pricing_source == "unresolved"
    assert row.enabled is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enabling_unresolved_zero_price_is_disabled(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """An explicit ``unresolved`` zero-price enable is persisted but forced
    disabled — never rejected, never billable."""
    provider_id = await _make_provider(integration_session)
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(
            provider_id,
            model_id="free-fall",
            prompt=0.0,
            completion=0.0,
            enabled=True,
            pricing_source="unresolved",
        ),
    )
    assert resp.status_code == 200
    row = await integration_session.get(ModelRow, ("free-fall", provider_id))
    assert row is not None
    assert row.pricing_source == "unresolved"
    assert row.enabled is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reenabling_unresolved_row_without_price_stays_disabled(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _make_provider(integration_session)
    headers = _admin_headers()
    # Seed a fail-closed row: unresolved, zero price, disabled.
    row = ModelRow(
        id="needs-price",
        name="Needs Price",
        description="d",
        created=0,
        context_length=4096,
        architecture=json.dumps(
            {
                "modality": "text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "unknown",
                "instruct_type": None,
            }
        ),
        pricing=json.dumps({"prompt": 0.0, "completion": 0.0}),
        upstream_provider_id=provider_id,
        enabled=False,
        forwarded_model_id="needs-price",
        pricing_source="unresolved",
    )
    integration_session.add(row)
    await integration_session.commit()

    # Flipping enabled=True while still zero-priced is accepted but stays
    # disabled: the source is preserved as unresolved, so it can't be billed.
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_payload(
            provider_id, model_id="needs-price", prompt=0.0, completion=0.0
        ),
    )
    assert resp.status_code == 200
    await integration_session.refresh(row)
    assert row.pricing_source == "unresolved"
    assert row.enabled is False

    # Giving it a real price flips it to manual and enabling now succeeds.
    ok = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_payload(provider_id, model_id="needs-price", prompt=1e-7),
    )
    assert ok.status_code == 200
    await integration_session.refresh(row)
    assert row.pricing_source == "manual"
    assert row.enabled is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_operator_zeroing_price_keeps_enabled_state(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """An operator editing a real price down to zero owns that price (``manual``)
    and their ``enabled`` choice is never second-guessed — the model stays
    enabled at an explicit $0."""
    provider_id = await _make_provider(integration_session)
    headers = _admin_headers()
    # Seed a litellm-priced, enabled model.
    row = ModelRow(
        id="going-free",
        name="Going Free",
        description="d",
        created=0,
        context_length=4096,
        architecture=json.dumps(
            {
                "modality": "text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "unknown",
                "instruct_type": None,
            }
        ),
        pricing=json.dumps({"prompt": 2.8e-7, "completion": 4.2e-7}),
        upstream_provider_id=provider_id,
        enabled=True,
        forwarded_model_id="going-free",
        pricing_source="litellm",
        pricing_source_version="1.83.0",
    )
    integration_session.add(row)
    await integration_session.commit()

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_payload(
            provider_id,
            model_id="going-free",
            prompt=0.0,
            completion=0.0,
            enabled=True,
        ),
    )
    assert resp.status_code == 200
    await integration_session.refresh(row)
    assert row.pricing_source == "manual"  # a price edit → operator owns it
    assert row.enabled is True  # zeroing a price never flips enabled


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explicitly_free_manual_model_is_allowed(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A zero price the operator explicitly marks ``manual`` is a deliberate
    free-model declaration: it is enabled as requested, never auto-disabled."""
    provider_id = await _make_provider(integration_session)
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(
            provider_id,
            model_id="truly-free",
            prompt=0.0,
            completion=0.0,
            enabled=True,
            pricing_source="manual",
        ),
    )
    assert resp.status_code == 200
    row = await integration_session.get(ModelRow, ("truly-free", provider_id))
    assert row is not None
    assert row.pricing_source == "manual"
    assert row.enabled is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_zero_price_entry_disabled_without_blocking_siblings(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A zero-price non-manual entry in a batch is force-disabled in place; it
    no longer aborts the whole batch, so a priced sibling still lands enabled."""
    provider_id = await _make_provider(integration_session)
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/batch-override",
        headers=_admin_headers(),
        json={
            "models": [
                _payload(
                    provider_id,
                    model_id="batch-free",
                    prompt=0.0,
                    completion=0.0,
                    enabled=True,
                ),
                _payload(provider_id, model_id="batch-paid", enabled=True),
            ]
        },
    )
    assert resp.status_code == 200
    free_row = await integration_session.get(ModelRow, ("batch-free", provider_id))
    assert free_row is not None
    assert free_row.pricing_source == "unresolved"
    assert free_row.enabled is False
    paid_row = await integration_session.get(ModelRow, ("batch-paid", provider_id))
    assert paid_row is not None
    assert paid_row.enabled is True


# ---------------------------------------------------------------------------
# edge cases — string-typed prices and invalid provenance
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_string_typed_price_edit_flips_to_manual(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A price edit sent with string-typed rates (some JSON producers emit
    strings) must still flip provenance to manual — the stored JSON is parsed as
    float on read, so the edit check must interpret strings the same way or the
    operator's edit silently keeps a stale non-manual tag."""
    provider_id = await _make_provider(integration_session)
    headers = _admin_headers()
    await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_payload(provider_id, model_id="str-edit", pricing_source="litellm"),
    )
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=headers,
        json=_payload(
            provider_id,
            model_id="str-edit",
            pricing_source="litellm",
            pricing={
                "prompt": "9.9e-07",  # edited (was 1.4e-7), sent as a string
                "completion": "2.8e-07",
                "request": 0.0,
                "image": 0.0,
                "web_search": 0.0,
                "internal_reasoning": 0.0,
                "input_cache_read": 0.0,
                "input_cache_write": 0.0,
            },
        ),
    )
    assert resp.status_code == 200
    row = await integration_session.get(ModelRow, ("str-edit", provider_id))
    assert row is not None
    assert row.pricing_source == "manual"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_pricing_source_is_rejected(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """An unknown pricing_source is a client bug: reject it at the edge rather
    than persist a junk tag that reads back as None (silent provenance loss, and
    a would-be trusted $0 that never gets the guard it needs)."""
    provider_id = await _make_provider(integration_session)
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/models",
        headers=_admin_headers(),
        json=_payload(provider_id, model_id="bad-src", pricing_source="lite-llm"),
    )
    assert resp.status_code == 422
