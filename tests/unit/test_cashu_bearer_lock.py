import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncGenerator, cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.auth import validate_bearer_key
from routstr.core.db import ApiKey
from routstr.redemption_cache import CachedRedemptionFailure, redemption_negative_cache

TOKEN = "cashuAbearer_lock_regression"
KEY_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'keys.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session


class CountingGuard:
    def __init__(self) -> None:
        self.entries = 0
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[None, None]:
        self.entries += 1
        async with self.lock:
            yield


@pytest.mark.parametrize("bearer", [TOKEN, f"sk-{KEY_HASH}"])
@pytest.mark.parametrize("min_cost", [0, 100, 101])
async def test_existing_key_skips_guard_and_checks_balance(
    session: AsyncSession, bearer: str, min_cost: int
) -> None:
    session.add(ApiKey(hashed_key=KEY_HASH, balance=100))
    await session.commit()
    session.expunge_all()
    guard = CountingGuard()
    with (
        patch("routstr.auth.wallet_operation_guard", guard),
        patch("routstr.auth.deserialize_token_from_string"),
        patch("routstr.auth.credit_balance", new_callable=AsyncMock) as redeem,
    ):
        if min_cost > 100:
            with pytest.raises(HTTPException) as exc:
                await validate_bearer_key(bearer, session, min_cost=min_cost)
            assert exc.value.status_code == 402
            detail = cast(dict[str, dict[str, str]], exc.value.detail)
            assert detail["error"]["code"] == "insufficient_balance"
        else:
            key = await validate_bearer_key(bearer, session, min_cost=min_cost)
            assert key.hashed_key == KEY_HASH
            assert key.balance == 100
    assert guard.entries == 0
    redeem.assert_not_awaited()


@pytest.mark.parametrize("bearer", [TOKEN, f"sk-{KEY_HASH}"])
async def test_existing_key_updates_persist_and_override_negative_cache(
    session: AsyncSession, bearer: str
) -> None:
    session.add(
        ApiKey(
            hashed_key=KEY_HASH,
            balance=100,
            key_expiry_time=1,
            refund_address="old@example.com",
        )
    )
    await session.commit()
    session.expunge_all()
    redemption_negative_cache.put(
        KEY_HASH,
        CachedRedemptionFailure(
            status_code=400,
            error_type="token_already_spent",
            message="Cashu token already spent",
            code="cashu_token_already_spent",
        ),
    )
    guard = CountingGuard()
    with (
        patch("routstr.auth.wallet_operation_guard", guard),
        patch("routstr.auth.deserialize_token_from_string"),
    ):
        unchanged = await validate_bearer_key(bearer, session)
        assert unchanged.key_expiry_time == 1
        assert unchanged.refund_address == "old@example.com"
        await validate_bearer_key(bearer, session, "new@example.com", 2000000000)
        await session.commit()
    session.expunge_all()
    key = await session.get(ApiKey, KEY_HASH)
    assert key is not None
    assert key.key_expiry_time == 2000000000
    assert key.refund_address == "new@example.com"
    assert guard.entries == 0


async def _credit(token: str, key: ApiKey, session: AsyncSession) -> int:
    key.balance += 100
    await session.commit()
    return 100


async def test_new_key_enters_guard_after_finishing_lookup(
    session: AsyncSession,
) -> None:
    guard = CountingGuard()

    @asynccontextmanager
    async def check_transaction() -> AsyncGenerator[None, None]:
        assert not session.in_transaction()
        async with guard():
            yield

    with (
        patch("routstr.auth.wallet_operation_guard", check_transaction),
        patch(
            "routstr.auth.deserialize_token_from_string",
            return_value=SimpleNamespace(mint="http://mint:3338", unit="sat"),
        ),
        patch("routstr.auth.credit_balance", side_effect=_credit) as redeem,
    ):
        key = await validate_bearer_key(TOKEN, session)
    assert key.hashed_key == KEY_HASH
    assert key.balance == 100
    assert guard.entries == 1
    redeem.assert_awaited_once()


async def test_concurrent_first_use_rechecks_inside_guard(engine: AsyncEngine) -> None:
    guard = CountingGuard()
    both_waiting = asyncio.Event()
    waiting = 0

    @asynccontextmanager
    async def synchronized_guard() -> AsyncGenerator[None, None]:
        nonlocal waiting
        waiting += 1
        if waiting == 2:
            both_waiting.set()
        # Both callers must miss the initial lookup before either creates a key.
        await asyncio.wait_for(both_waiting.wait(), timeout=5)
        async with guard():
            yield

    async def validate() -> ApiKey:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await validate_bearer_key(TOKEN, session)

    with (
        patch("routstr.auth.wallet_operation_guard", synchronized_guard),
        patch(
            "routstr.auth.deserialize_token_from_string",
            return_value=SimpleNamespace(mint="http://mint:3338", unit="sat"),
        ),
        patch("routstr.auth.credit_balance", side_effect=_credit) as redeem,
    ):
        keys = await asyncio.wait_for(
            asyncio.gather(validate(), validate()), timeout=10
        )
    assert [key.hashed_key for key in keys] == [KEY_HASH, KEY_HASH]
    assert [key.balance for key in keys] == [100, 100]
    assert guard.entries == 2
    redeem.assert_awaited_once()
