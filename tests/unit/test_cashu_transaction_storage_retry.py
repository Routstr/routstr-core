from unittest.mock import AsyncMock, patch

import pytest

from routstr.core import db


@pytest.mark.asyncio
async def test_cashu_transaction_storage_retries_then_succeeds() -> None:
    store = AsyncMock(side_effect=[OSError("database locked"), True])
    sleep = AsyncMock()

    with (
        patch("routstr.core.db.store_cashu_transaction", store),
        patch("routstr.core.db.asyncio.sleep", sleep),
    ):
        stored = await db.store_cashu_transaction_with_retry(
            token="cashuAretry",
            amount=100,
            unit="sat",
        )

    assert stored is True
    assert store.await_count == 2
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_cashu_transaction_storage_raises_after_bounded_retries() -> None:
    error = OSError("database unavailable")
    store = AsyncMock(side_effect=error)
    sleep = AsyncMock()

    with (
        patch("routstr.core.db.store_cashu_transaction", store),
        patch("routstr.core.db.asyncio.sleep", sleep),
        patch("routstr.core.db.logger.critical") as critical,
    ):
        with pytest.raises(OSError, match="database unavailable"):
            await db.store_cashu_transaction_with_retry(
                token="cashuAfail",
                amount=100,
                unit="sat",
                max_attempts=3,
            )

    assert store.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]
    critical.assert_called_once()
