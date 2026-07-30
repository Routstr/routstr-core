"""Integration coverage for proxy database-session lifetime."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr import proxy as proxy_module
from routstr.core.db import ApiKey


@pytest.mark.asyncio
async def test_authenticated_proxy_releases_db_connection_before_upstream_headers(
    integration_engine: AsyncEngine,
    integration_session: AsyncSession,
    patched_db_engine: None,
) -> None:
    """Slow upstream header waits must not retain a checked-out DB connection."""
    key = ApiKey(
        hashed_key="proxy-pool-key",
        balance=1_000_000,
        refund_mint_url="http://primary:3338",
        refund_currency="sat",
    )
    integration_session.add(key)
    await integration_session.commit()

    request = MagicMock()
    request.method = "POST"
    request.headers = {"authorization": "Bearer test-key"}
    request.body = AsyncMock(return_value=json.dumps({"model": "test-model"}).encode())
    request.url.path = "/v1/chat/completions"
    request.state.request_id = "pool-hold-regression"

    model = MagicMock()
    upstream = MagicMock()
    upstream.provider_type = "test"
    upstream.prepare_headers.return_value = {}

    async def wait_for_headers(*args: object, **kwargs: object) -> Response:
        assert integration_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        return Response(status_code=200)

    upstream.forward_request = AsyncMock(side_effect=wait_for_headers)

    with (
        patch("routstr.proxy.get_candidates", return_value=[(model, upstream)]),
        patch("routstr.proxy.get_max_cost_for_model", AsyncMock(return_value=100)),
        patch(
            "routstr.proxy.calculate_discounted_max_cost",
            AsyncMock(return_value=100),
        ),
        patch("routstr.proxy.check_token_balance"),
        patch("routstr.proxy.get_bearer_token_key", AsyncMock(return_value=key)),
    ):
        response = await proxy_module._proxy(
            request, "v1/chat/completions", integration_session
        )

    assert response.status_code == 200
