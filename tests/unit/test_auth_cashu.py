import hashlib
from types import SimpleNamespace
from typing import AsyncGenerator, cast
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


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_message"),
    [
        (
            ValueError("Mint Error: Token already spent. (Code: 11001)"),
            400,
            "Cashu token already spent",
        ),
        (
            ValueError(
                "Token amount (5 sat) is insufficient to cover melt fees. "
                "Needed: 7 sat (amount: 5 + fee: 1 + input_fees: 1)"
            ),
            400,
            "Token value is too small to cover swap fees",
        ),
        (
            ValueError(
                "Failed to estimate fees: Fees (7 sat) exceed token amount (5 sat)"
            ),
            400,
            "Token value is too small to cover swap fees",
        ),
        (
            ValueError(
                "Failed to melt token from foreign mint http://foreign:3338: boom"
            ),
            400,
            "Failed to swap token from foreign mint",
        ),
        (
            ValueError("could not decode token"),
            401,
            "Invalid Cashu token",
        ),
        (
            ValueError("some unexpected wallet condition"),
            400,
            "Failed to redeem Cashu token",
        ),
    ],
)
@pytest.mark.asyncio
async def test_redemption_failure_returns_sanitized_error(
    session: AsyncSession,
    error: Exception,
    expected_status: int,
    expected_message: str,
) -> None:
    """Redemption failures share one error code, expose stable sanitized
    messages (no raw exception text), and leave no orphan ApiKey row."""
    token = "cashuAredemption_fails_with_specific_error"
    hashed_key = hashlib.sha256(token.encode()).hexdigest()
    token_obj = SimpleNamespace(mint="http://mint:3338", unit="sat")

    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch("routstr.auth.deserialize_token_from_string", return_value=token_obj),
        patch(
            "routstr.auth.credit_balance",
            new=AsyncMock(side_effect=error),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await validate_bearer_key(token, session)

    assert exc_info.value.status_code == expected_status
    detail = cast(dict[str, dict[str, str]], exc_info.value.detail)
    error_detail = detail["error"]
    assert error_detail["code"] == "token_redemption_failed"
    assert error_detail["message"] == expected_message
    assert str(error) not in error_detail["message"]
    assert await session.get(ApiKey, hashed_key) is None


@pytest.mark.asyncio
async def test_unexpected_redemption_error_returns_internal_error(
    session: AsyncSession,
) -> None:
    """Unexpected (non-wallet) failures surface as generic 500s without
    leaking internal details, instead of masquerading as token errors."""
    token = "cashuAredemption_fails_with_internal_error"
    hashed_key = hashlib.sha256(token.encode()).hexdigest()
    token_obj = SimpleNamespace(mint="http://mint:3338", unit="sat")

    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch("routstr.auth.deserialize_token_from_string", return_value=token_obj),
        patch(
            "routstr.auth.credit_balance",
            new=AsyncMock(side_effect=RuntimeError("db exploded at /var/lib/secret")),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await validate_bearer_key(token, session)

    assert exc_info.value.status_code == 500
    detail = cast(dict[str, dict[str, str]], exc_info.value.detail)
    error_detail = detail["error"]
    assert error_detail["code"] == "internal_error"
    assert "/var/lib/secret" not in error_detail["message"]
    assert await session.get(ApiKey, hashed_key) is None


@pytest.mark.asyncio
async def test_internal_error_with_invalid_keyword_does_not_masquerade(
    session: AsyncSession,
) -> None:
    """A non-wallet fault whose text merely contains "invalid" (but not
    "token") must fall through to a generic 500, not a 401 token error.

    Guards the anchored `"invalid"/"decode"` + `"token"` gate against stdlib/
    driver strings like "Invalid isoformat string" leaking as token errors."""
    token = "cashuAinternal_fault_mentions_invalid"
    hashed_key = hashlib.sha256(token.encode()).hexdigest()
    token_obj = SimpleNamespace(mint="http://mint:3338", unit="sat")

    from routstr.core.settings import settings

    with (
        patch.object(settings, "cashu_mints", ["http://mint:3338"]),
        patch("routstr.auth.deserialize_token_from_string", return_value=token_obj),
        patch(
            "routstr.auth.credit_balance",
            new=AsyncMock(
                side_effect=RuntimeError("Invalid isoformat string: '2020-13-99'")
            ),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await validate_bearer_key(token, session)

    assert exc_info.value.status_code == 500
    detail = cast(dict[str, dict[str, str]], exc_info.value.detail)
    assert detail["error"]["code"] == "internal_error"
    assert await session.get(ApiKey, hashed_key) is None
