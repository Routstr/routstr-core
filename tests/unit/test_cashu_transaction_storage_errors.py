from unittest.mock import AsyncMock, patch

import pytest

from routstr.core.db import store_cashu_transaction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        OSError("disk full"),
        RuntimeError("connection lost"),
        ConnectionRefusedError("database unavailable"),
    ],
)
async def test_store_cashu_transaction_propagates_commit_errors(
    error: Exception,
) -> None:
    session = AsyncMock()
    session.commit.side_effect = error
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    with patch("routstr.core.db.create_session", return_value=session):
        with pytest.raises(type(error), match=str(error)):
            await store_cashu_transaction(
                token="cashuAtest",
                amount=1_000,
                unit="sat",
                mint_url="https://mint.example",
                typ="out",
                request_id="request-1",
            )


@pytest.mark.asyncio
async def test_store_cashu_transaction_returns_true_after_commit() -> None:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None

    with patch("routstr.core.db.create_session", return_value=session):
        stored = await store_cashu_transaction(
            token="cashuAtest",
            amount=1_000,
            unit="sat",
        )

    assert stored is True
    session.commit.assert_awaited_once()
