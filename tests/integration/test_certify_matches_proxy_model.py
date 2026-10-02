"""Certification must send the upstream the model id the proxy sends.

An admin alias row has ``id`` and ``forwarded_model_id`` that differ. The
proxy forwards ``transform_model_name(model.id)`` (``prepare_request_body``),
so a probe that sends the forwarded id certifies a request no client can make.
"""

import json
import time
from typing import Any
from unittest.mock import patch

import pytest
import respx
from httpx import AsyncClient, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey
from routstr.proxy import reinitialize_upstreams

from .test_certify_endpoint import _admin_headers, _make_provider, _model_row


@pytest.mark.integration
@pytest.mark.asyncio
@respx.mock
async def test_certify_and_proxy_send_the_same_model(
    integration_session: AsyncSession,
    integration_client: AsyncClient,
) -> None:
    base_url = "https://certify-upstream.example/v1"
    respx.get(f"{base_url}/models").mock(return_value=Response(200, json={"data": []}))
    chat = respx.post(f"{base_url}/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )
    )
    provider = await _make_provider(integration_session)
    model = _model_row(provider.id, model_id="row-id")  # type: ignore[arg-type]
    model.forwarded_model_id = "client-alias"
    integration_session.add(model)
    integration_session.add(
        ApiKey(
            hashed_key="certify-contract", balance=10**9, created_at=int(time.time())
        )
    )
    await integration_session.commit()

    with (
        patch("routstr.payment.models.sats_usd_price", return_value=0.0005),
        patch("routstr.payment.cost_calculation.sats_usd_price", return_value=0.0005),
        patch("routstr.payment.price.SATS_USD_PRICE", 0.0005),
    ):
        await reinitialize_upstreams()
        proxied = await integration_client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-certify-contract"},
            json={
                "model": "client-alias",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
        )
        assert proxied.status_code == 200, proxied.text
        certified = await integration_client.post(
            f"/admin/api/upstream-providers/{provider.id}/certify",
            headers=_admin_headers(),
            json={"model_id": "row-id", "check_cache": False},
        )
        assert certified.status_code == 200, certified.text

    bodies: list[dict[str, Any]] = [
        json.loads(call.request.content) for call in chat.calls
    ]
    assert len(bodies) == 2
    proxy_model, certify_model = bodies[0]["model"], bodies[1]["model"]
    assert certify_model == proxy_model
