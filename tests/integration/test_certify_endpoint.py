"""Integration tests for POST /admin/api/upstream-providers/{id}/certify.

Exercises the endpoint against a mocked upstream (no real network, no
real spend). The test fixtures create a provider + model row in the
integration DB, then mock the two HTTP calls the probe makes (GET /models
and POST /chat/completions) with ``respx`` so every verdict — ok, warn,
fail — is reachable deterministically.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
import respx
from httpx import AsyncClient, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.admin import admin_sessions
from routstr.core.db import ModelRow, UpstreamProviderRow
from routstr.proxy import reinitialize_upstreams


# The conftest patches ``routstr.payment.price.sats_usd_price``, but
# ``cost_calculation.py`` and ``models.py`` import it as a local binding
# the conftest-level patch cannot reach. Pin it here so every test that
# goes through ``_row_to_model`` or ``calculate_cost`` gets a real sats
# price — same pattern as ``test_model_price_propagation.py``.
@pytest.fixture(autouse=True)
def _pin_sats_usd() -> Any:
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        with patch(
            "routstr.payment.cost_calculation.sats_usd_price", return_value=0.0005
        ):
            with patch("routstr.payment.price.SATS_USD_PRICE", 0.0005):
                yield


ARCHITECTURE = {
    "modality": "text",
    "input_modalities": ["text"],
    "output_modalities": ["text"],
    "tokenizer": "unknown",
    "instruct_type": None,
}


def _pricing(**overrides: float) -> dict[str, Any]:
    pricing: dict[str, Any] = {
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


def _admin_headers() -> dict[str, str]:
    token = "test-certify-token"
    admin_sessions[token] = int(
        (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_provider(
    session: AsyncSession,
    *,
    slug: str | None = None,
    provider_fee: float = 1.0,
    base_url: str = "https://certify-upstream.example/v1",
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


def _model_row(provider_id: int, **overrides: Any) -> ModelRow:
    model_id = overrides.pop("model_id", "cert-test-model")
    return ModelRow(
        id=model_id,
        name=model_id,
        description="d",
        created=0,
        context_length=8192,
        architecture=json.dumps(ARCHITECTURE),
        pricing=json.dumps(_pricing(**overrides.pop("pricing_overrides", {}))),
        upstream_provider_id=provider_id,
        enabled=True,
        forwarded_model_id=model_id,
    )


async def _seed_and_init(
    session: AsyncSession,
    client: AsyncClient,
    *,
    provider_fee: float = 1.0,
    model_id: str = "cert-test-model",
    pricing_overrides: dict[str, Any] | None = None,
    base_url: str = "https://certify-upstream.example/v1",
) -> int:
    provider = await _make_provider(
        session, provider_fee=provider_fee, base_url=base_url
    )
    assert provider.id is not None
    session.add(
        _model_row(
            provider.id,
            model_id=model_id,
            pricing_overrides=pricing_overrides or {},
        )
    )
    await session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()
    return provider.id


def _find_row(rows: list[dict[str, Any]], row_id: str) -> dict[str, Any]:
    for row in rows:
        if row["id"] == row_id:
            return row
    raise AssertionError(f"row {row_id!r} not found")


def _mock_models_response(
    base_url: str = "https://certify-upstream.example/v1",
    models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if models is None:
        models = [{"id": "cert-test-model"}]
    return {"data": models}


def _mock_chat_response(
    model: str = "cert-test-model",
    prompt_tokens: int = 5,
    completion_tokens: int = 1,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_certify_requires_admin_auth(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider = await _make_provider(integration_session)
    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider.id}/certify", json={}
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_certify_unknown_provider_returns_404(
    integration_client: AsyncClient,
) -> None:
    resp = await integration_client.post(
        "/admin/api/upstream-providers/999999999/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_all_ok(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _seed_and_init(integration_session, integration_client)

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(200, json=_mock_models_response())
    )
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(200, json=_mock_chat_response())
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "rows" in body
    assert "checklist" in body

    # All live rows should be ok
    live_row_ids = [
        "endpoint.validity",
        "endpoint.reachable",
        "endpoint.models_payload",
        "usage.capture",
        "cost.prompt_completion",
    ]
    for row_id in live_row_ids:
        row = _find_row(body["rows"], row_id)
        assert row["status"] == "ok", f"{row_id}: {row}"

    # All checklist goals should be ok
    for item in body["checklist"]:
        assert item["status"] == "ok", f"{item['goal']}: {item}"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_heartbeat_fail_on_500(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _seed_and_init(integration_session, integration_client)

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(500, json={"error": "internal"})
    )
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(200, json=_mock_chat_response())
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    row = _find_row(body["rows"], "endpoint.reachable")
    assert row["status"] == "fail"
    assert row["evidence"]["status_code"] == 500

    # heartbeat goal should be fail
    heartbeat_goal = next(
        item for item in body["checklist"] if item["goal"] == "heartbeat"
    )
    assert heartbeat_goal["status"] == "fail"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_heartbeat_fail_on_transport_error(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _seed_and_init(integration_session, integration_client)

    respx.get("https://certify-upstream.example/v1/models").mock(
        side_effect=__import__("httpx").ConnectError("connection refused")
    )
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(200, json=_mock_chat_response())
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    row = _find_row(body["rows"], "endpoint.reachable")
    assert row["status"] == "fail"
    assert row["evidence"]["error"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_models_payload_fail_on_malformed(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _seed_and_init(integration_session, integration_client)

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(200, json={"error": "no data field"})
    )
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(200, json=_mock_chat_response())
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    row = _find_row(body["rows"], "endpoint.models_payload")
    assert row["status"] == "fail"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_usage_warn_when_no_usage(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _seed_and_init(integration_session, integration_client)

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(200, json=_mock_models_response())
    )
    # No "usage" key in the chat response
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "id": "x",
                "model": "cert-test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    usage_row = _find_row(body["rows"], "usage.capture")
    assert usage_row["status"] == "warn"

    cost_row = _find_row(body["rows"], "cost.prompt_completion")
    assert cost_row["status"] == "warn"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_usage_fail_on_non_2xx(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _seed_and_init(integration_session, integration_client)

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(200, json=_mock_models_response())
    )
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(401, json={"error": {"message": "invalid api key"}})
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    row = _find_row(body["rows"], "usage.capture")
    assert row["status"] == "fail"
    assert "401" in row["detail"]


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_cost_ok_with_token_pricing(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _seed_and_init(
        integration_session,
        integration_client,
        provider_fee=1.0,
        pricing_overrides={"prompt": 1e-7, "completion": 2e-7},
    )

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(200, json=_mock_models_response())
    )
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(
            200, json=_mock_chat_response(prompt_tokens=10, completion_tokens=5)
        )
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    row = _find_row(body["rows"], "cost.prompt_completion")
    assert row["status"] == "ok", row
    assert row["evidence"]["basis"] == "configured_token_pricing"
    assert row["evidence"]["input_tokens"] == 10
    assert row["evidence"]["output_tokens"] == 5


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_cost_ok_with_usd_reported(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _seed_and_init(
        integration_session,
        integration_client,
        provider_fee=1.05,
    )

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(200, json=_mock_models_response())
    )
    chat_payload = _mock_chat_response()
    chat_payload["usage"]["cost_details"] = {"total_cost": 0.0001}
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(200, json=chat_payload)
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    row = _find_row(body["rows"], "cost.prompt_completion")
    assert row["status"] == "ok", row
    assert row["evidence"]["basis"] == "upstream_reported_usd"
    assert row["evidence"]["reported_usd"] == 0.0001


@pytest.mark.integration
@pytest.mark.asyncio
async def test_certify_with_no_served_model(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """When the provider has no model that is being served (e.g. all have
    unusable pricing), the live checks should be skipped as warn, not
    crash."""
    provider = await _make_provider(integration_session)
    assert provider.id is not None
    # A negative prompt price makes has_usable_pricing() return False,
    # withholding the model from the served map.
    session_add = _model_row(
        provider.id, model_id="bad-model", pricing_overrides={"prompt": -1.0}
    )
    integration_session.add(session_add)
    await integration_session.commit()
    with patch("routstr.payment.models.sats_usd_price", return_value=0.0005):
        await reinitialize_upstreams()

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider.id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Live rows should be warn (skipped)
    for row_id in ["endpoint.reachable", "usage.capture", "cost.prompt_completion"]:
        row = _find_row(body["rows"], row_id)
        assert row["status"] == "warn", f"{row_id}: {row}"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_with_explicit_model_id(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    provider_id = await _seed_and_init(
        integration_session,
        integration_client,
        model_id="explicit-model",
    )

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(
            200, json=_mock_models_response(models=[{"id": "explicit-model"}])
        )
    )
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(200, json=_mock_chat_response(model="explicit-model"))
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={"model_id": "explicit-model"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    usage_row = _find_row(body["rows"], "usage.capture")
    assert usage_row["status"] == "ok"


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_includes_pricing_rows(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """The certify response should also carry the four pricing.* rows from
    the read-only report, so the certification is self-contained."""
    provider_id = await _seed_and_init(integration_session, integration_client)

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(200, json=_mock_models_response())
    )
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(200, json=_mock_chat_response())
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    row_ids = [row["id"] for row in body["rows"]]
    assert "pricing.served_matches_configured" in row_ids
    assert "pricing.sats_pricing_present" in row_ids
    assert "pricing.enabled_models_served" in row_ids
    assert "pricing.cache_rate" in row_ids


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_row_contract_shape(
    integration_client: AsyncClient, integration_session: AsyncSession
) -> None:
    """Every row in the response has the required keys and a valid status."""
    provider_id = await _seed_and_init(integration_session, integration_client)

    respx.get("https://certify-upstream.example/v1/models").mock(
        return_value=Response(200, json=_mock_models_response())
    )
    respx.post("https://certify-upstream.example/v1/chat/completions").mock(
        return_value=Response(200, json=_mock_chat_response())
    )

    resp = await integration_client.post(
        f"/admin/api/upstream-providers/{provider_id}/certify",
        headers=_admin_headers(),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert "provider_id" in body
    assert "generated_at" in body
    assert "rows" in body
    assert "checklist" in body

    for row in body["rows"]:
        assert set(row) >= {"id", "status", "title", "detail", "evidence"}
        assert row["status"] in {"ok", "warn", "fail"}

    for item in body["checklist"]:
        assert set(item) >= {"goal", "label", "status", "tick", "rows"}
        assert item["status"] in {"ok", "warn", "fail"}
        assert item["tick"] in {"☑️", "⚠️", "❌"}
