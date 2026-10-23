from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routstr import balance as balance_module
from routstr.core.db import get_session


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


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_create_balance_response_is_not_cached(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """The response carries a spendable API key (and the GET URL a token)."""
    key = SimpleNamespace(hashed_key="hashed", balance=1_000)
    monkeypatch.setattr(
        balance_module, "validate_bearer_key", AsyncMock(return_value=key)
    )

    async def override_get_session():  # type: ignore[no-untyped-def]
        yield AsyncMock()

    app = FastAPI()
    app.include_router(balance_module.balance_router)
    app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        if method == "GET":
            response = await client.get(
                "/v1/balance/create", params={"initial_balance_token": "cashuAx"}
            )
        else:
            response = await client.post(
                "/v1/balance/create", json={"initial_balance_token": "cashuAx"}
            )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
