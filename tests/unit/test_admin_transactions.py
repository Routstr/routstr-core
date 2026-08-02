from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.core.admin import get_transactions_api
from routstr.core.db import CashuTransaction


@pytest.mark.asyncio
async def test_transactions_api_excludes_internal_sweep_claim_timestamp() -> None:
    transaction = CashuTransaction(
        token="cashu-token",
        amount=10,
        unit="sat",
        type="out",
        sweep_started_at=123,
    )
    count_result = MagicMock()
    count_result.one.return_value = 1
    transactions_result = MagicMock()
    transactions_result.all.return_value = [transaction]
    session = MagicMock()
    session.exec = AsyncMock(side_effect=[count_result, transactions_result])

    @asynccontextmanager
    async def create_session():  # type: ignore[no-untyped-def]
        yield session

    with patch("routstr.core.admin.create_session", create_session):
        response = await get_transactions_api()

    assert response["total"] == 1
    assert response["transactions"][0]["token"] == "cashu-token"
    assert "sweep_started_at" not in response["transactions"][0]
