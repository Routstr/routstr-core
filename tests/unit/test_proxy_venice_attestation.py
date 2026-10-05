import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from httpx import ASGITransport, AsyncClient

from routstr import proxy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["tee/attestation", "tee/signature", "v1/tee/attestation/", "v1/tee/signature/"],
)
async def test_venice_metadata_uses_model_candidate_and_native_id(monkeypatch, path):
    venice = MagicMock(provider_type="venice")
    venice.prepare_headers.return_value = {}
    venice.transform_model_name.return_value = "native-model"
    venice.forward_get_request = AsyncMock(return_value=Response("{}", status_code=200))
    tinfoil = MagicMock(provider_type="tinfoil")
    tinfoil.forward_get_request = AsyncMock()
    model = MagicMock(id="venice/native-model")
    monkeypatch.setattr(proxy, "_upstreams", [tinfoil, venice])
    monkeypatch.setattr(
        proxy,
        "get_candidates",
        lambda name: [(model, venice)] if name == "alias" else [],
    )
    app = FastAPI()
    app.include_router(proxy.proxy_router)
    query = (
        {"request_id": "chatcmpl-test"} if "signature" in path else {"nonce": "a" * 64}
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/{path}", params={"model": "alias", **query})
    assert response.status_code == 200
    request = venice.forward_get_request.call_args.args[0]
    assert await asyncio.wait_for(request.body(), timeout=1) == b""
    assert request.query_params["model"] == "native-model"
    for key, value in query.items():
        assert request.query_params[key] == value
    tinfoil.forward_get_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["missing", ""])
async def test_signature_requires_known_venice_model(monkeypatch, model):
    tinfoil = MagicMock(provider_type="tinfoil")
    tinfoil.forward_get_request = AsyncMock()
    monkeypatch.setattr(proxy, "_upstreams", [tinfoil])
    monkeypatch.setattr(proxy, "get_candidates", lambda _: [])
    app = FastAPI()
    app.include_router(proxy.proxy_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/tee/signature", params={"model": model})
    assert response.status_code == 404
    tinfoil.forward_get_request.assert_not_awaited()
