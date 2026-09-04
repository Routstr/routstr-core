"""Certification report for a configured upstream provider.

Covers ``GET /admin/api/upstream-providers/{provider_id}/report``: the row
contract shape, and the four pricing rows it carries —
``pricing.served_matches_configured``, ``pricing.sats_pricing_present``,
``pricing.enabled_models_served`` and ``pricing.cache_rate``. Each row is
computed from the DB row plus the in-process served map; none of them make a
network call.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import admin_sessions
from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.proxy import reinitialize_upstreams

PRICING_ROW_IDS = (
    "pricing.served_matches_configured",
    "pricing.sats_pricing_present",
    "pricing.enabled_models_served",
    "pricing.cache_rate",
)

ARCHITECTURE = {
    "modality": "text",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "tokenizer": "unknown",
    "instruct_type": None,
}


def _admin_headers() -> dict[str, str]:
    token = "test-admin-upstream-report-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_provider(
    session: AsyncSession,
    *,
    slug: str | None = None,
    provider_fee: float = 1.0,
    base_url: str = "https://report-upstream.example/v1",
    api_key: str = "test-key",
) -> UpstreamProviderRow:
    provider = UpstreamProviderRow(
        provider_type="generic",
        base_url=base_url,
        api_key=api_key,
        provider_fee=provider_fee,
        slug=slug,
    )
    session.add(provider)
    await session.commit()
    await session.refresh(provider)
    assert provider.id is not None
    return provider


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


def _model_row(
    provider_id: int,
    *,
    model_id: str,
    pricing: dict[str, object],
    enabled: bool = True,
) -> ModelRow:
    return ModelRow(
        id=model_id,
        name=model_id,
        description="d",
        created=0,
        context_length=8192,
        architecture=json.dumps(ARCHITECTURE),
        pricing=json.dumps(pricing),
        upstream_provider_id=provider_id,
        enabled=enabled,
        # A self-alias, same as the admin write edge stores by default —
        # ``get_effective_forwarded_model_id`` treats this as "no distinct
        # forwarded id" so it does not register a second routable alias.
        forwarded_model_id=model_id,
    )


def _row_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [row["id"] for row in rows]


def _find_row(rows: list[dict[str, Any]], row_id: str) -> dict[str, Any]:
    for row in rows:
        if row["id"] == row_id:
            return row
    raise AssertionError(f"row {row_id!r} not found in {_row_ids(rows)!r}")


def _pid(provider: UpstreamProviderRow) -> int:
    """Narrow a persisted row's optional primary key for typed call sites."""
    assert provider.id is not None
    return provider.id


async def _get_report(client: AsyncClient, provider_ref: str | int) -> Any:
    return await client.get(
        f"/admin/api/upstream-providers/{provider_ref}/report",
        headers=_admin_headers(),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_report_requires_admin_auth(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Sanity check: the report sits behind the same gate as the rest of
    ``core/admin.py``. This already passes against the not-implemented stub
    because ``require_admin_api`` runs as a dependency before the route body
    — it is included for completeness, not as a red proof.
    """
    provider = await _make_provider(integration_session)

    resp = await integration_client.get(
        f"/admin/api/upstream-providers/{_pid(provider)}/report"
    )

    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_report_unknown_provider_returns_404(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    resp = await _get_report(integration_client, 999_999_999)

    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_report_row_contract_shape_and_order(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """The row contract from the report-contract spec: stable top-level keys,
    a fixed row order with the four pricing rows first, and every row
    carrying id/status/title/detail/evidence with status in {ok, warn, fail}.
    """
    provider = await _make_provider(integration_session, slug="report-shape-provider")
    integration_session.add(
        _model_row(_pid(provider), model_id="shape-model", pricing=_pricing())
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    assert provider.slug is not None
    resp = await _get_report(integration_client, provider.slug)

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The numeric id, not an echo of whatever ref (slug, here) the request
    # used to look the provider up — matches ``_serialize_provider``'s "id".
    assert body["provider_id"] == provider.id
    generated_at = body["generated_at"]
    # Must parse as an ISO-8601 timestamp; a trailing "Z" is not accepted by
    # ``fromisoformat`` on its own.
    parsed_generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    # Freshly generated, not a stale cached/hardcoded value.
    assert abs((datetime.now(timezone.utc) - parsed_generated_at).total_seconds()) < 60

    rows = body["rows"]
    assert _row_ids(rows)[:4] == list(PRICING_ROW_IDS)
    for row in rows:
        assert set(row) >= {"id", "status", "title", "detail", "evidence"}
        assert row["status"] in {"ok", "warn", "fail"}
        assert isinstance(row["title"], str) and row["title"]
        assert isinstance(row["detail"], str) and row["detail"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_served_matches_configured_ok_when_prices_agree(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider = await _make_provider(integration_session, provider_fee=1.05)
    integration_session.add(
        _model_row(
            _pid(provider),
            model_id="agree-model",
            pricing=_pricing(prompt=2e-7, completion=4e-7),
        )
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.served_matches_configured")
    assert row["status"] == "ok", row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_served_matches_configured_zero_vs_zero_is_ok(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A price of zero on both sides is agreement, not a legitimacy check."""
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(
            _pid(provider),
            model_id="free-model",
            pricing=_pricing(prompt=0.0, completion=0.0),
        )
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.served_matches_configured")
    assert row["status"] == "ok", row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_served_matches_configured_fails_on_stale_served_map(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Drift the DB row without refreshing the served map — the same shape
    of staleness a writer that bypasses ``core/admin.py`` would leave behind.
    "Configured" (built fresh from the row) must then disagree with "served"
    (built earlier, still in-process) with no epsilon.
    """
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(
            _pid(provider),
            model_id="drift-model",
            pricing=_pricing(prompt=1e-7, completion=2e-7),
        )
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    stored = await integration_session.get(ModelRow, ("drift-model", _pid(provider)))
    assert stored is not None
    stored.pricing = json.dumps(_pricing(prompt=9e-7, completion=2e-7))
    integration_session.add(stored)
    await integration_session.commit()
    # Deliberately no reinitialize_upstreams() here: the served map must stay
    # stale for this to be a meaningful drift case.

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.served_matches_configured")
    assert row["status"] == "fail", row
    assert row["evidence"] is not None
    assert "drift-model" in json.dumps(row["evidence"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sats_pricing_present_ok_when_conversion_succeeds(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(_pid(provider), model_id="sats-ok-model", pricing=_pricing())
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.sats_pricing_present")
    assert row["status"] == "ok", row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sats_pricing_present_fails_when_btc_feed_is_swallowed(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """``_update_model_sats_pricing`` swallows every exception and leaves the
    served model with ``sats_pricing=None``. This must surface here rather
    than silently advertising models with no sats price.
    """
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(_pid(provider), model_id="sats-fail-model", pricing=_pricing())
    )
    await integration_session.commit()
    with patch(
        "routstr.payment.models.sats_usd_price",
        side_effect=RuntimeError("btc feed unavailable"),
    ):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.sats_pricing_present")
    assert row["status"] == "fail", row
    assert "sats-fail-model" in json.dumps(row["evidence"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enabled_models_served_ok_when_all_enabled_models_are_served(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(_pid(provider), model_id="served-model", pricing=_pricing())
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.enabled_models_served")
    assert row["status"] == "ok", row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enabled_models_served_fails_when_enabled_model_has_unusable_pricing(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A negative rate makes ``has_usable_pricing`` false, so the algorithm
    withholds the model from the served map even though the DB row is
    enabled — exactly the "enabled but never served" case this row exists
    to catch, and it must not require an upstream that stopped listing the
    model to reproduce.
    """
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(
            _pid(provider),
            model_id="unusable-price-model",
            pricing=_pricing(prompt=-1.0),
        )
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.enabled_models_served")
    assert row["status"] == "fail", row
    assert "unusable-price-model" in json.dumps(row["evidence"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_report_survives_a_model_row_that_fails_to_parse(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """ "A row never throws": a stored row even malformed enough that
    ``_build_model_from_row`` raises on it (bad JSON, in this case — the same
    shape of corruption a legacy writer can leave) must become a ``fail`` row
    with the exception described, not a 500 that takes out the whole report.
    """
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(_pid(provider), model_id="good-model", pricing=_pricing())
    )
    broken = _model_row(_pid(provider), model_id="broken-model", pricing=_pricing())
    broken.pricing = "{not valid json"
    integration_session.add(broken)
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _row_ids(body["rows"])[:4] == list(PRICING_ROW_IDS)
    row = _find_row(body["rows"], "pricing.served_matches_configured")
    assert row["status"] == "fail", row
    assert "broken-model" in json.dumps(row["evidence"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cache_rate_ignores_an_enabled_model_that_is_not_served(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A negative price holds a model back from the served map even though
    its row is enabled (see ``test_enabled_models_served_fails_when_...``).
    ``pricing.cache_rate`` must not certify a cache rate for a model that
    isn't actually being served — it should skip it, not count it, and
    certainly not report ``ok`` for a model nothing will ever bill through.
    """
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(
            _pid(provider),
            model_id="unserved-model",
            pricing=_pricing(prompt=-1.0),
        )
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.cache_rate")
    assert row["status"] == "ok", row
    assert row["evidence"]["checked"] == 0
    assert "unserved-model" not in json.dumps(row["evidence"])


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["gpt-4o", "deepseek-chat"])
async def test_cache_rate_warns_when_backfill_only_supplies_the_read_rate(
    integration_client: AsyncClient,
    integration_session: AsyncSession,
    model_id: str,
) -> None:
    """The row is computed from ``backfill_cache_pricing(row.id, pricing)`` at
    serve time, not from the raw DB row. Both ``gpt-4o`` and DeepSeek chat
    models are stored with ``input_cache_read=0`` (the OpenRouter feed omits
    it) and litellm's cost map fills that in — reading the raw row instead
    would falsely flag the read rate as unknown, which is the defect this
    row's spec was corrected to avoid.

    litellm's cost map has no ``cache_creation_input_token_cost`` entry for
    either model, so the write rate stays unbackfilled: the row must still
    ``warn`` (a real, if partial, gap) rather than call this ``ok``.
    """
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(
            _pid(provider),
            model_id=model_id,
            pricing=_pricing(prompt=2.5e-6, completion=1e-5, input_cache_read=0.0),
        )
    )
    await integration_session.commit()

    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.cache_rate")
    assert row["status"] == "warn", row
    evidence_text = json.dumps(row["evidence"])
    assert model_id in evidence_text
    assert "input_cache_write" in evidence_text
    assert "input_cache_read" not in evidence_text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cache_rate_ok_when_backfill_supplies_both_rates(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """``claude-sonnet-4-5`` has both a cache-read and a cache-creation
    (write) rate in litellm's cost map, so once both are backfilled the row
    must be ``ok`` — this is the counterpart to the partial-coverage case
    above, proving ``ok`` is reachable and not just a status the row never
    returns once both rates are checked.
    """
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(
            _pid(provider),
            model_id="claude-sonnet-4-5",
            pricing=_pricing(prompt=3e-6, completion=1.5e-5, input_cache_read=0.0),
        )
    )
    await integration_session.commit()

    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.cache_rate")
    assert row["status"] == "ok", row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cache_rate_warns_when_rate_missing_and_unknown_to_litellm(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """No cache rate, and litellm has never heard of the model: the report
    has no persisted probe result yet (that lands with the cost probe), so
    this must be ``warn``, never ``fail`` — ``fail`` needs the probe to know
    the upstream is token-billed.
    """
    provider = await _make_provider(integration_session)
    integration_session.add(
        _model_row(
            _pid(provider),
            model_id="totally-custom-self-hosted-model",
            pricing=_pricing(),
        )
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.cache_rate")
    assert row["status"] == "warn", row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_report_rows_are_scoped_to_the_requested_provider(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """A second provider's broken model must not leak into this provider's
    aggregate row — each row is scoped to the provider named in the URL.
    """
    provider_a = await _make_provider(integration_session, slug="scope-provider-a")
    provider_b = await _make_provider(
        integration_session,
        slug="scope-provider-b",
        base_url="https://report-upstream-b.example/v1",
        api_key="test-key-b",
    )

    integration_session.add(
        _model_row(
            _pid(provider_a),
            model_id="scope-a-model",
            pricing=_pricing(prompt=1e-7, completion=2e-7),
        )
    )
    integration_session.add(
        _model_row(
            _pid(provider_b),
            model_id="scope-b-model",
            pricing=_pricing(prompt=-1.0),
        )
    )
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await _get_report(integration_client, _pid(provider_a))

    assert resp.status_code == 200, resp.text
    row = _find_row(resp.json()["rows"], "pricing.enabled_models_served")
    assert row["status"] == "ok", row
    # Evidence must actually be inspectable here, not merely absent — an "ok"
    # row that reports ``evidence: None`` would make the leak check below
    # vacuously true (``"x" not in json.dumps(None)`` is always True) instead
    # of proving provider_b's model never entered provider_a's row.
    assert row["evidence"] is not None
    assert "scope-b-model" not in json.dumps(row["evidence"])
