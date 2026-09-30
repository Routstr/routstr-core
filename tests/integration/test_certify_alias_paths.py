"""Exact certification paths retain the provider model's forwarded identity."""

import json
from typing import Any
from unittest.mock import patch

import pytest
import respx
from httpx import AsyncClient, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ModelPathRow
from routstr.proxy import reinitialize_upstreams
from routstr.upstream.model_paths import encode_model_path
from tests.integration.test_certify_endpoint import (
    _admin_headers,
    _make_provider,
    _model_row,
)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded", ["anthropic/claude-opus-4.6", "remote-id"])
@pytest.mark.parametrize("enabled", [True, False])
@respx.mock
async def test_certify_forwarded_alias_listed_path_succeeds(
    integration_session: AsyncSession,
    integration_client: AsyncClient,
    forwarded: str,
    enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr import proxy

    for name in ("_upstreams", "_provider_map", "_unique_models"):
        monkeypatch.setattr(proxy, name, getattr(proxy, name).copy())
    base_url = "https://certify-upstream.example/v1"
    respx.get(f"{base_url}/models").mock(return_value=Response(200, json={"data": []}))
    chat = respx.post(f"{base_url}/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "model": forwarded,
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )
    )
    provider = await _make_provider(integration_session)
    model = _model_row(provider.id, model_id="local-alias")  # type: ignore[arg-type]
    model.forwarded_model_id = forwarded
    model.enabled = enabled
    model_path = encode_model_path(base_url, forwarded, "endpoint")
    integration_session.add(model)
    integration_session.add(
        ModelPathRow(
            upstream_provider_id=provider.id,
            model_id=forwarded,
            path=model_path,
            endpoint_tag="endpoint",
            provider_slug="mock",
            provider_type="generic",
        )
    )
    other_id = f"other/{forwarded.rsplit('/', 1)[-1]}"
    other_path = encode_model_path(base_url, other_id, "other-endpoint")
    integration_session.add(
        ModelPathRow(
            upstream_provider_id=provider.id,
            model_id=other_id,
            path=other_path,
            endpoint_tag="other-endpoint",
            provider_slug="mock",
            provider_type="generic",
        )
    )
    await integration_session.commit()
    with (
        patch("routstr.payment.models.sats_usd_price", return_value=0.0005),
        patch("routstr.payment.cost_calculation.sats_usd_price", return_value=0.0005),
        patch("routstr.payment.price.SATS_USD_PRICE", 0.0005),
    ):
        await reinitialize_upstreams()
        listed = await integration_client.get(
            f"/admin/api/upstream-providers/{provider.id}/models",
            headers=_admin_headers(),
        )
        assert listed.status_code == 200, listed.text
        assert (
            listed.json()["certification_paths"]["local-alias"][0]["path"] == model_path
        )
        response = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/certify",
            headers=_admin_headers(),
            json={
                "model_id": "local-alias",
                "model_path": model_path,
                "check_cache": False,
            },
        )
        if not enabled:
            assert response.status_code == 400, response.text
            assert chat.call_count == 0
            return
        assert response.status_code == 200, response.text
        assert chat.call_count == 1
        body: dict[str, Any] = json.loads(chat.calls[0].request.content)
        assert body["model"] == forwarded
        assert body["provider"] == {"order": ["endpoint"], "allow_fallbacks": False}
        mismatch = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/certify",
            headers=_admin_headers(),
            json={
                "model_id": "wrong-alias",
                "model_path": model_path,
                "check_cache": False,
            },
        )
        assert mismatch.status_code == 400
        wrong_prefix = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/certify",
            headers=_admin_headers(),
            json={
                "model_id": "local-alias",
                "model_path": other_path,
                "check_cache": False,
            },
        )
        assert wrong_prefix.status_code == 400
        assert chat.call_count == 1
