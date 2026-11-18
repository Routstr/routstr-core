from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient, Response

from routstr import balance as balance_module
from routstr.core.db import get_session
from routstr.core.exceptions import (
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from routstr.core.middleware import NoStoreMiddleware


@pytest.mark.asyncio
async def test_create_balance_accepts_large_cashu_token_in_post_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "cashuA" + "x" * 20_000
    key = SimpleNamespace(hashed_key="hashed", balance=123_000)
    validate_bearer_key = AsyncMock(return_value=key)
    session = AsyncMock()
    monkeypatch.setattr(balance_module, "validate_bearer_key", validate_bearer_key)

    async def override_get_session():  # type: ignore[no-untyped-def]
        yield session

    app = FastAPI()
    app.include_router(balance_module.balance_router)
    app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/balance/create",
            json={"initial_balance_token": token},
        )

    assert response.status_code == 200
    assert response.json() == {"api_key": "sk-hashed", "balance": 123_000}
    validate_bearer_key.assert_awaited_once_with(token, session)


def _app_with_handlers() -> FastAPI:
    app = FastAPI()
    app.add_middleware(NoStoreMiddleware)
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)
    app.include_router(balance_module.balance_router)
    app.include_router(balance_module.deprecated_wallet_router)

    async def override_get_session():  # type: ignore[no-untyped-def]
        yield AsyncMock()

    app.dependency_overrides[get_session] = override_get_session
    return app


def _assert_no_store(response: Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("prefix", ["/v1/balance", "/v1/wallet"])
async def test_create_balance_response_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, method: str, prefix: str
) -> None:
    """The response carries a spendable API key (and the GET URL a token)."""
    key = SimpleNamespace(hashed_key="hashed", balance=1_000)
    monkeypatch.setattr(
        balance_module, "validate_bearer_key", AsyncMock(return_value=key)
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app_with_handlers()),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        if method == "GET":
            response = await client.get(
                f"{prefix}/create", params={"initial_balance_token": "cashuAx"}
            )
        else:
            response = await client.post(
                f"{prefix}/create", json={"initial_balance_token": "cashuAx"}
            )

    assert response.status_code == 200
    _assert_no_store(response)


@pytest.mark.asyncio
async def test_failed_create_balance_response_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token stays in the GET URL when redemption fails."""
    monkeypatch.setattr(
        balance_module,
        "validate_bearer_key",
        AsyncMock(side_effect=HTTPException(status_code=400, detail="spent")),
    )

    async with AsyncClient(
        transport=ASGITransport(app=_app_with_handlers()),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/balance/create", params={"initial_balance_token": "cashuAx"}
        )

    assert response.status_code == 400
    _assert_no_store(response)


@pytest.mark.asyncio
async def test_invalid_create_balance_request_is_not_cached() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app_with_handlers()),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/balance/create",
            params={"initial_balance_token": "cashuAx", "validity_date": "soon"},
        )

    assert response.status_code == 422
    _assert_no_store(response)


@pytest.mark.asyncio
async def test_server_error_on_balance_route_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starlette answers unhandled errors outside the app middleware stack."""
    monkeypatch.setattr(
        balance_module,
        "validate_bearer_key",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    async with AsyncClient(
        transport=ASGITransport(  # type: ignore[arg-type]
            app=_app_with_handlers(), raise_app_exceptions=False
        ),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/balance/create", params={"initial_balance_token": "cashuAx"}
        )

    assert response.status_code == 500
    _assert_no_store(response)


@pytest.mark.asyncio
async def test_other_routes_keep_default_cache_headers() -> None:
    app = FastAPI()
    app.add_middleware(NoStoreMiddleware)

    @app.get("/v1/balancer")
    async def lookalike() -> dict:
        return {}

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/balancer")

    assert response.status_code == 200
    assert "cache-control" not in response.headers
    assert "referrer-policy" not in response.headers
