from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import release_reservation
from routstr.core.db import ApiKey
from routstr.upstream.base import BaseUpstreamProvider


@pytest.mark.asyncio
async def test_release_reservation_clears_reserved_balance() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    key = ApiKey(hashed_key="key", balance=1_000, reserved_balance=500)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(key)
        await session.commit()

        assert await release_reservation(key, session, 500) is True
        await session.refresh(key)
        assert key.reserved_balance == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_billing_error_releases_reservation_and_propagates() -> None:
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test-key"
    )

    async def aiter_bytes() -> AsyncGenerator[bytes, None]:
        yield b"data: [DONE]\n\n"

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = {"content-type": "text/event-stream"}
    upstream_response.aiter_bytes = aiter_bytes

    key = MagicMock(spec=ApiKey)
    key.hashed_key = "test-key-hash"
    session = MagicMock()
    session.get = AsyncMock(return_value=key)
    session.rollback = AsyncMock()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    release = AsyncMock(return_value=True)

    with (
        patch(
            "routstr.upstream.base.adjust_payment_for_tokens",
            AsyncMock(side_effect=SQLAlchemyError("database unavailable")),
        ),
        patch("routstr.upstream.base.release_reservation", release),
        patch("routstr.upstream.base.create_session", return_value=session_context),
    ):
        response = await provider.handle_streaming_chat_completion(
            response=upstream_response,
            key=key,
            max_cost_for_model=500,
            background_tasks=MagicMock(),
        )

        with pytest.raises(SQLAlchemyError, match="database unavailable"):
            async for _ in response.body_iterator:
                pass

    session.rollback.assert_awaited_once()
    release.assert_awaited_once_with(key, session, 500)
