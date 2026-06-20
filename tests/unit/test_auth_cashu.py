import hashlib
from types import SimpleNamespace
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import validate_bearer_key
from routstr.core.db import ApiKey


def _make_engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    db_session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db_session
    finally:
        await db_session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_first_cashu_redemption_rolls_back_empty_api_key(
    session: AsyncSession,
) -> None:
    token = "cashuAfirst_seen_but_redemption_fails"
    hashed_key = hashlib.sha256(token.encode()).hexdigest()
    token_obj = SimpleNamespace(mint="http://mint:3338", unit="sat")

    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch("routstr.auth.deserialize_token_from_string", return_value=token_obj),
        patch(
            "routstr.auth.credit_balance",
            new=AsyncMock(side_effect=ValueError("token already spent")),
        ),
    ):
        with pytest.raises(HTTPException):
            await validate_bearer_key(token, session)

    assert await session.get(ApiKey, hashed_key) is None
