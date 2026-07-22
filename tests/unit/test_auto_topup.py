import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.core.db import CashuTransaction
from routstr.upstream.auto_topup import _check_and_topup


def _row() -> MagicMock:
    row = MagicMock()
    row.id = "provider-1"
    row.base_url = "https://provider.test"
    row.api_key = "secret"
    row.provider_settings = json.dumps(
        {
            "auto_topup": True,
            "topup_threshold": 100,
            "topup_amount_limit": 50,
            "topup_mint_url": "https://mint.test",
        }
    )
    return row


class _Session:
    def __init__(self, transaction: CashuTransaction) -> None:
        self.transaction = transaction
        self.commit = AsyncMock()

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def exec(self, query: object) -> MagicMock:
        result = MagicMock()
        result.first.return_value = self.transaction
        return result

    def add(self, transaction: CashuTransaction) -> None:
        self.transaction = transaction


@pytest.mark.asyncio
async def test_auto_topup_persists_before_sending_and_marks_success_collected() -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=0)
    provider.topup = AsyncMock(return_value={"balance": 50})
    transaction = CashuTransaction(
        token="cashu-token", amount=50, unit="sat", source="auto_topup"
    )
    session = _Session(transaction)

    with (
        patch(
            "routstr.upstream.auto_topup.RoutstrUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch(
            "routstr.upstream.auto_topup.send_token",
            AsyncMock(return_value="cashu-token"),
        ),
        patch(
            "routstr.upstream.auto_topup.store_cashu_transaction",
            AsyncMock(return_value=True),
        ) as store,
        patch(
            "routstr.upstream.auto_topup.token_mint_url",
            return_value="https://fallback-mint.test",
        ),
        patch("routstr.upstream.auto_topup.create_session", return_value=session),
    ):
        await _check_and_topup(_row())

    store.assert_awaited_once_with(
        token="cashu-token",
        amount=50,
        unit="sat",
        mint_url="https://fallback-mint.test",
        typ="out",
        collected=False,
        source="auto_topup",
    )
    provider.topup.assert_awaited_once_with("cashu-token")
    assert transaction.collected is True
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [{"error": "rejected"}, RuntimeError("network")])
async def test_auto_topup_failure_leaves_persisted_token_uncollected(
    outcome: object,
) -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=0)
    provider.topup = AsyncMock(
        side_effect=outcome if isinstance(outcome, Exception) else None,
        return_value=outcome,
    )

    with (
        patch(
            "routstr.upstream.auto_topup.RoutstrUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch(
            "routstr.upstream.auto_topup.send_token",
            AsyncMock(return_value="cashu-token"),
        ),
        patch(
            "routstr.upstream.auto_topup.store_cashu_transaction",
            AsyncMock(return_value=True),
        ),
        patch("routstr.upstream.auto_topup.create_session") as create_session,
    ):
        if isinstance(outcome, Exception):
            with pytest.raises(RuntimeError):
                await _check_and_topup(_row())
        else:
            await _check_and_topup(_row())

    create_session.assert_not_called()


@pytest.mark.asyncio
async def test_auto_topup_does_not_send_untracked_token() -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=0)
    provider.topup = AsyncMock()
    with (
        patch(
            "routstr.upstream.auto_topup.RoutstrUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch(
            "routstr.upstream.auto_topup.send_token",
            AsyncMock(return_value="cashu-token"),
        ),
        patch(
            "routstr.upstream.auto_topup.store_cashu_transaction",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch(
            "routstr.upstream.auto_topup.release_token_reservation",
            AsyncMock(),
        ) as reclaim,
    ):
        await _check_and_topup(_row())

    reclaim.assert_awaited_once_with("cashu-token")
    provider.topup.assert_not_awaited()
