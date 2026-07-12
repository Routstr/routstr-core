import json
from unittest.mock import AsyncMock, Mock, call

import pytest

from routstr.core.db import UpstreamProviderRow
from routstr.upstream import auto_topup


def _row() -> UpstreamProviderRow:
    return UpstreamProviderRow(
        id=7,
        provider_type="routstr",
        base_url="https://provider.example",
        api_key="secret",
        provider_settings=json.dumps(
            {
                "auto_topup": True,
                "topup_threshold": 100,
                "topup_amount_limit": 250,
                "topup_mint_url": "https://mint.example",
            }
        ),
    )


def _configure(
    monkeypatch: pytest.MonkeyPatch, topup_result: object
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    provider = Mock()
    provider.get_balance = AsyncMock(return_value=50_000)
    if isinstance(topup_result, Exception):
        provider.topup = AsyncMock(side_effect=topup_result)
    else:
        provider.topup = AsyncMock(return_value=topup_result)

    send_token = AsyncMock(return_value="cashuBoutgoing")
    store_transaction = AsyncMock(return_value=True)
    mark_collected = AsyncMock()

    monkeypatch.setattr(
        auto_topup.RoutstrUpstreamProvider,
        "from_db_row",
        Mock(return_value=provider),
    )
    monkeypatch.setattr(auto_topup, "send_token", send_token)
    monkeypatch.setattr(auto_topup, "store_cashu_transaction", store_transaction)
    monkeypatch.setattr(auto_topup, "_mark_topup_collected", mark_collected)
    return provider.topup, store_transaction, mark_collected


@pytest.mark.asyncio
async def test_auto_topup_marks_persisted_token_collected_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topup, store_transaction, mark_collected = _configure(monkeypatch, {"ok": True})

    await auto_topup._check_and_topup(_row())

    store_transaction.assert_awaited_once_with(
        token="cashuBoutgoing",
        amount=250,
        unit="sat",
        mint_url="https://mint.example",
        typ="out",
        collected=False,
        source="auto_topup",
    )
    topup.assert_awaited_once_with("cashuBoutgoing")
    mark_collected.assert_awaited_once_with("cashuBoutgoing")


@pytest.mark.asyncio
async def test_auto_topup_leaves_persisted_token_uncollected_on_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topup, store_transaction, mark_collected = _configure(
        monkeypatch, {"error": "rejected"}
    )

    await auto_topup._check_and_topup(_row())

    assert store_transaction.await_args_list == [
        call(
            token="cashuBoutgoing",
            amount=250,
            unit="sat",
            mint_url="https://mint.example",
            typ="out",
            collected=False,
            source="auto_topup",
        )
    ]
    topup.assert_awaited_once_with("cashuBoutgoing")
    mark_collected.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_topup_leaves_persisted_token_uncollected_on_provider_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topup, store_transaction, mark_collected = _configure(
        monkeypatch, RuntimeError("upstream unavailable")
    )

    with pytest.raises(RuntimeError, match="upstream unavailable"):
        await auto_topup._check_and_topup(_row())

    store_transaction.assert_awaited_once()
    topup.assert_awaited_once_with("cashuBoutgoing")
    mark_collected.assert_not_awaited()
