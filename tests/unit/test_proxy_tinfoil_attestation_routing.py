from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from httpx import ASGITransport, AsyncClient

from routstr import proxy as proxy_module


@pytest.fixture
def proxy_app() -> FastAPI:
    app = FastAPI()
    app.include_router(proxy_module.proxy_router)
    return app


@pytest.mark.asyncio
async def test_attestation_get_routes_directly_to_tinfoil_provider(
    monkeypatch: pytest.MonkeyPatch, proxy_app: FastAPI
) -> None:
    non_tinfoil = MagicMock()
    non_tinfoil.provider_type = "openai"
    non_tinfoil.prepare_headers = MagicMock(return_value={})
    non_tinfoil.forward_get_request = AsyncMock(
        return_value=Response(status_code=404, content=b"wrong upstream")
    )

    tinfoil = MagicMock()
    tinfoil.provider_type = "tinfoil"
    tinfoil.prepare_headers = MagicMock(return_value={"accept": "application/json"})
    tinfoil.forward_get_request = AsyncMock(
        return_value=Response(status_code=200, content=b'{"attestation":true}')
    )

    monkeypatch.setattr(proxy_module, "_upstreams", [non_tinfoil, tinfoil])

    async with AsyncClient(
        transport=ASGITransport(app=proxy_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/attestation")

    assert response.status_code == 200
    assert response.content == b'{"attestation":true}'
    non_tinfoil.forward_get_request.assert_not_called()
    tinfoil.forward_get_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_tee_attestation_get_routes_directly_to_tinfoil_provider(
    monkeypatch: pytest.MonkeyPatch, proxy_app: FastAPI
) -> None:
    non_tinfoil = MagicMock()
    non_tinfoil.provider_type = "openrouter"
    non_tinfoil.prepare_headers = MagicMock(return_value={})
    non_tinfoil.forward_get_request = AsyncMock(
        return_value=Response(status_code=404, content=b"wrong upstream")
    )

    tinfoil = MagicMock()
    tinfoil.provider_type = "tinfoil"
    tinfoil.prepare_headers = MagicMock(return_value={"accept": "application/json"})
    tinfoil.forward_get_request = AsyncMock(
        return_value=Response(status_code=200, content=b'{"tee":true}')
    )

    monkeypatch.setattr(proxy_module, "_upstreams", [non_tinfoil, tinfoil])

    async with AsyncClient(
        transport=ASGITransport(app=proxy_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/tee/attestation")

    assert response.status_code == 200
    assert response.content == b'{"tee":true}'
    non_tinfoil.forward_get_request.assert_not_called()
    tinfoil.forward_get_request.assert_awaited_once()


@pytest.mark.parametrize("path", ["attestation/", "tee/attestation/"])
@pytest.mark.asyncio
async def test_attestation_trailing_slash_routes_directly_to_tinfoil(
    monkeypatch: pytest.MonkeyPatch, proxy_app: FastAPI, path: str
) -> None:
    tinfoil = MagicMock()
    tinfoil.provider_type = "tinfoil"
    tinfoil.prepare_headers = MagicMock(return_value={})
    tinfoil.forward_get_request = AsyncMock(return_value=Response(status_code=200))
    monkeypatch.setattr(proxy_module, "_upstreams", [tinfoil])

    async with AsyncClient(
        transport=ASGITransport(app=proxy_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get(f"/{path}")

    assert response.status_code == 200
    tinfoil.forward_get_request.assert_awaited_once()


@pytest.mark.parametrize("path", ["attestation/foo", "attestationjunk"])
@pytest.mark.asyncio
async def test_non_attestation_prefix_does_not_bypass_authentication(
    monkeypatch: pytest.MonkeyPatch, proxy_app: FastAPI, path: str
) -> None:
    tinfoil = MagicMock()
    tinfoil.provider_type = "tinfoil"
    tinfoil.forward_get_request = AsyncMock()
    monkeypatch.setattr(proxy_module, "_upstreams", [tinfoil])

    async with AsyncClient(
        transport=ASGITransport(app=proxy_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get(f"/{path}")

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_model"
    tinfoil.forward_get_request.assert_not_awaited()


def test_attestation_upstream_selection_is_tinfoil_only() -> None:
    non_tinfoil = MagicMock(provider_type="openai")
    tinfoil = MagicMock(provider_type="tinfoil")

    assert proxy_module._select_unauthenticated_get_upstreams(
        "attestation", [non_tinfoil, tinfoil]
    ) == [tinfoil]
    assert proxy_module._select_unauthenticated_get_upstreams(
        "tee/attestation", [non_tinfoil, tinfoil]
    ) == [tinfoil]
    assert proxy_module._select_unauthenticated_get_upstreams(
        "attestation/", [non_tinfoil, tinfoil]
    ) == [tinfoil]
    assert proxy_module._select_unauthenticated_get_upstreams(
        "attestationjunk", [non_tinfoil, tinfoil]
    ) == [non_tinfoil, tinfoil]
