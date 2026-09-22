from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from httpx import ASGITransport, AsyncClient

from routstr import proxy as proxy_module
from routstr.core.error_scope import (
    ERROR_SCOPE_HEADER,
    ERROR_SCOPE_UPSTREAM,
    UPSTREAM_ERROR_STATUS,
    UPSTREAM_UNAVAILABLE,
)


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


@pytest.mark.parametrize(
    "path",
    [
        # A valid `attestation` segment is not the exact attestation route, and
        # `attestation` takes no id segment, so the endpoint allowlist rejects
        # it at the edge rather than letting it reach model/auth handling.
        "attestation/foo",
        # Not a known endpoint at all: rejected at the edge before routing.
        "attestationjunk",
    ],
)
@pytest.mark.asyncio
async def test_non_attestation_prefix_does_not_bypass_authentication(
    monkeypatch: pytest.MonkeyPatch,
    proxy_app: FastAPI,
    path: str,
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

    assert response.status_code == 404
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


# --------------------------------------------------------------------------- #
# Acceptance: CORE-UPSTREAM-5XX-NOT-NODE-DOWN on the unauthenticated GET path
#
# An upstream 5xx on these paths must be reported as 424 + UPSTREAM_UNAVAILABLE
# with the X-Routstr-Error-Scope: upstream header, must stay retryable across
# candidates, and must never be re-labelled as a node fault.
# --------------------------------------------------------------------------- #


def _attributed_424() -> Response:
    """The response a provider hands back for an upstream-attributed 5xx."""
    import json as _json

    return Response(
        content=_json.dumps(
            {
                "error": {
                    "type": "upstream_error",
                    "code": UPSTREAM_UNAVAILABLE,
                    "message": "Attestation upstream returned 503",
                    "upstream_status": 503,
                }
            }
        ).encode(),
        status_code=UPSTREAM_ERROR_STATUS,
        media_type="application/json",
        headers={ERROR_SCOPE_HEADER: ERROR_SCOPE_UPSTREAM},
    )


def _attestation_provider(forward: AsyncMock) -> MagicMock:
    provider = MagicMock()
    provider.provider_type = "tinfoil"
    provider.prepare_headers = MagicMock(return_value={})
    provider.forward_get_request = forward
    return provider


@pytest.mark.asyncio
async def test_unauthenticated_get_returns_attributed_424_when_all_fail(
    monkeypatch: pytest.MonkeyPatch, proxy_app: FastAPI
) -> None:
    tinfoil = _attestation_provider(AsyncMock(return_value=_attributed_424()))
    monkeypatch.setattr(proxy_module, "_upstreams", [tinfoil])

    async with AsyncClient(
        transport=ASGITransport(app=proxy_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/attestation")

    assert response.status_code == UPSTREAM_ERROR_STATUS
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    payload = json.loads(response.content)
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["upstream_status"] == 503


@pytest.mark.asyncio
async def test_unauthenticated_get_fails_over_past_an_attributed_424(
    monkeypatch: pytest.MonkeyPatch, proxy_app: FastAPI
) -> None:
    """An upstream-attributed 424 stays retryable: the caller sees the healthy
    provider's response and never the upstream error."""
    failing = _attestation_provider(AsyncMock(return_value=_attributed_424()))
    healthy = _attestation_provider(
        AsyncMock(return_value=Response(status_code=200, content=b'{"ok":true}'))
    )
    monkeypatch.setattr(proxy_module, "_upstreams", [failing, healthy])

    async with AsyncClient(
        transport=ASGITransport(app=proxy_app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/attestation")

    assert response.status_code == 200
    assert response.content == b'{"ok":true}'
    failing.forward_get_request.assert_awaited_once()
    healthy.forward_get_request.assert_awaited_once()
    assert ERROR_SCOPE_HEADER not in response.headers


@pytest.mark.asyncio
async def test_attestation_host_5xx_is_attributed_to_the_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Tinfoil attestation hop itself maps its 5xx to 424 + upstream scope."""
    from routstr.upstream.tinfoil import TinfoilUpstreamProvider

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def get(self, _url: str, headers: dict | None = None) -> httpx.Response:
            return httpx.Response(status_code=503, content=b"atc down")

    monkeypatch.setattr(
        "routstr.upstream.tinfoil.httpx.AsyncClient", lambda **_kw: _FakeClient()
    )
    provider = TinfoilUpstreamProvider(api_key="k")

    response = await provider._proxy_attestation({})

    assert response.status_code == UPSTREAM_ERROR_STATUS
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    payload = json.loads(bytes(response.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["upstream_status"] == 503


@pytest.mark.asyncio
async def test_attestation_host_4xx_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr.upstream.tinfoil import TinfoilUpstreamProvider

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def get(self, _url: str, headers: dict | None = None) -> httpx.Response:
            return httpx.Response(status_code=404, content=b"missing")

    monkeypatch.setattr(
        "routstr.upstream.tinfoil.httpx.AsyncClient", lambda **_kw: _FakeClient()
    )
    provider = TinfoilUpstreamProvider(api_key="k")

    response = await provider._proxy_attestation({})

    assert response.status_code == 404
    assert bytes(response.body) == b"missing"
