import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr.core.db import CashuTransaction
from routstr.upstream.auto_topup import (
    _check_and_topup,
    _parse_ppq_request_id,
    validate_auto_topup_settings,
)
from routstr.upstream.ppqai import PPQAIUpstreamProvider


def test_ppq_claim_parser_rejects_invalid_expiry() -> None:
    assert _parse_ppq_request_id("ppq:operation:not-a-timestamp:invoice") is None


@pytest.mark.asyncio
async def test_ppq_balance_rejects_boolean_api_value() -> None:
    provider = PPQAIUpstreamProvider("secret")
    provider.check_balance = AsyncMock(return_value={"balance": False})  # type: ignore[method-assign]

    assert await provider.get_balance() is None


def _row() -> MagicMock:
    row = MagicMock()
    row.id = "provider-1"
    row.base_url = "https://provider.test"
    row.api_key = "secret"
    row.provider_type = "routstr"
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
        patch("routstr.upstream.auto_topup.create_session", return_value=session),
    ):
        await _check_and_topup(_row())

    store.assert_awaited_once_with(
        token="cashu-token",
        amount=50,
        unit="sat",
        mint_url="https://mint.test",
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
    ):
        await _check_and_topup(_row())
    provider.topup.assert_not_awaited()


def _ppq_row() -> MagicMock:
    row = MagicMock()
    row.id = "ppq-provider-1"
    row.base_url = "https://api.ppq.ai"
    row.api_key = "secret"
    row.provider_type = "ppqai"
    row.provider_settings = json.dumps(
        {
            "auto_topup": True,
            "topup_threshold": 5.0,
            "topup_amount_limit": 10,
        }
    )
    return row


@pytest.mark.asyncio
async def test_ppq_auto_topup_pays_invoice_and_confirms_settlement() -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=2.5)
    provider.initiate_topup = AsyncMock(
        return_value=MagicMock(
            invoice_id="invoice-1",
            payment_request="lnbc-invoice",
            amount=10,
            currency="USD",
            expires_at=None,
        )
    )
    provider.check_topup_status = AsyncMock(return_value=True)
    plan = MagicMock()
    plan.invoice_amount_sats = 100
    plan.maximum_spend_sats = 102
    plan.quote.amount = 100
    plan.quote.fee_reserve = 2
    plan.mint_url = "https://mint-rich.test"
    plan.unit = "sat"
    row = _ppq_row()

    with (
        patch(
            "routstr.upstream.auto_topup.PPQAIUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch(
            "routstr.upstream.auto_topup._reconcile_ppq_state",
            AsyncMock(return_value=False),
        ),
        patch(
            "routstr.upstream.auto_topup._claim_ppq_topup",
            AsyncMock(return_value="operation-1"),
        ),
        patch(
            "routstr.upstream.auto_topup.prepare_bolt11_payment",
            AsyncMock(return_value=plan),
        ) as prepare,
        patch(
            "routstr.upstream.auto_topup.execute_bolt11_payment",
            AsyncMock(return_value=(101, "https://mint-rich.test", "sat")),
        ) as execute,
        patch(
            "routstr.upstream.auto_topup._record_ppq_invoice", AsyncMock()
        ) as record,
        patch(
            "routstr.upstream.auto_topup._set_ppq_state_terminal", AsyncMock()
        ) as terminal,
        patch("routstr.upstream.auto_topup.sats_usd_price", return_value=0.001),
    ):
        await _check_and_topup(row)

    provider.initiate_topup.assert_awaited_once_with(10)
    prepare.assert_awaited_once_with("lnbc-invoice")
    execute.assert_awaited_once_with(plan)
    record.assert_awaited_once()
    provider.check_topup_status.assert_awaited_once_with("invoice-1")
    terminal.assert_awaited_once_with(
        row, "operation-1", collected=True, swept=False
    )


@pytest.mark.asyncio
async def test_ppq_ambiguous_melt_keeps_claim_and_emits_critical_alert() -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=2.5)
    provider.initiate_topup = AsyncMock(
        return_value=MagicMock(
            invoice_id="invoice-1",
            payment_request="lnbc-invoice",
            amount=10,
            currency="USD",
            expires_at=None,
        )
    )
    plan = MagicMock(maximum_spend_sats=102, mint_url="https://mint.test", unit="sat")
    plan.quote.amount = 100
    plan.quote.fee_reserve = 2
    row = _ppq_row()

    with (
        patch(
            "routstr.upstream.auto_topup.PPQAIUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch(
            "routstr.upstream.auto_topup._reconcile_ppq_state",
            AsyncMock(return_value=False),
        ),
        patch(
            "routstr.upstream.auto_topup._claim_ppq_topup",
            AsyncMock(return_value="operation-1"),
        ),
        patch(
            "routstr.upstream.auto_topup.prepare_bolt11_payment",
            AsyncMock(return_value=plan),
        ),
        patch(
            "routstr.upstream.auto_topup.execute_bolt11_payment",
            AsyncMock(side_effect=TimeoutError("ambiguous melt")),
        ),
        patch(
            "routstr.upstream.auto_topup._record_ppq_invoice",
            AsyncMock(return_value=2_000_000_000),
        ),
        patch(
            "routstr.upstream.auto_topup._mark_ppq_reconcile", AsyncMock()
        ) as reconcile_mark,
        patch(
            "routstr.upstream.auto_topup._set_ppq_state_terminal", AsyncMock()
        ) as terminal,
        patch("routstr.upstream.auto_topup.sats_usd_price", return_value=0.001),
        patch("routstr.upstream.auto_topup.logger.critical") as critical,
    ):
        with pytest.raises(TimeoutError, match="ambiguous melt"):
            await _check_and_topup(row)

    # The claim is never released — it moves to reconcile for the admin.
    terminal.assert_not_awaited()
    reconcile_mark.assert_awaited_once()
    critical.assert_called_once()
    assert "admin reconciliation" in critical.call_args.args[0]


@pytest.mark.asyncio
async def test_ppq_auto_topup_skips_when_balance_meets_threshold() -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=5.0)
    provider.initiate_topup = AsyncMock()

    with (
        patch(
            "routstr.upstream.auto_topup.PPQAIUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch(
            "routstr.upstream.auto_topup._reconcile_ppq_state",
            AsyncMock(return_value=False),
        ),
    ):
        await _check_and_topup(_ppq_row())

    provider.initiate_topup.assert_not_awaited()


@pytest.mark.asyncio
async def test_ppq_pending_attempt_suppresses_duplicate_topup() -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock()

    with (
        patch(
            "routstr.upstream.auto_topup.PPQAIUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch(
            "routstr.upstream.auto_topup._reconcile_ppq_state",
            AsyncMock(return_value=True),
        ),
    ):
        await _check_and_topup(_ppq_row())

    provider.get_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_ppq_auto_topup_rejects_non_finite_balance() -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=float("nan"))

    with (
        patch(
            "routstr.upstream.auto_topup.PPQAIUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch(
            "routstr.upstream.auto_topup._reconcile_ppq_state",
            AsyncMock(return_value=False),
        ),
        patch("routstr.upstream.auto_topup._claim_ppq_topup", AsyncMock()) as claim,
    ):
        await _check_and_topup(_ppq_row())

    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_routstr_threshold_is_compared_in_sats() -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=100)
    provider.topup = AsyncMock()

    with (
        patch(
            "routstr.upstream.auto_topup.RoutstrUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch("routstr.upstream.auto_topup.send_token", AsyncMock()) as send,
    ):
        await _check_and_topup(_row())

    send.assert_not_awaited()
    provider.topup.assert_not_awaited()


@pytest.mark.asyncio
async def test_settled_topup_alerts_when_its_claim_was_already_released() -> None:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=2.5)
    provider.initiate_topup = AsyncMock(
        return_value=MagicMock(
            invoice_id="invoice-1",
            payment_request="lnbc-invoice",
            amount=10,
            currency="USD",
            expires_at=None,
        )
    )
    provider.check_topup_status = AsyncMock(return_value=True)
    plan = MagicMock()
    plan.maximum_spend_sats = 102
    plan.quote.amount = 100
    plan.quote.fee_reserve = 2
    plan.mint_url = "https://mint-rich.test"
    plan.unit = "sat"

    with (
        patch(
            "routstr.upstream.auto_topup.PPQAIUpstreamProvider.from_db_row",
            return_value=provider,
        ),
        patch(
            "routstr.upstream.auto_topup._reconcile_ppq_state",
            AsyncMock(return_value=False),
        ),
        patch(
            "routstr.upstream.auto_topup._claim_ppq_topup",
            AsyncMock(return_value="operation-1"),
        ),
        patch(
            "routstr.upstream.auto_topup.prepare_bolt11_payment",
            AsyncMock(return_value=plan),
        ),
        patch(
            "routstr.upstream.auto_topup.execute_bolt11_payment",
            AsyncMock(return_value=(101, "https://mint-rich.test", "sat")),
        ),
        patch("routstr.upstream.auto_topup._record_ppq_invoice", AsyncMock()),
        patch(
            "routstr.upstream.auto_topup._set_ppq_state_terminal",
            AsyncMock(return_value=False),
        ),
        patch("routstr.upstream.auto_topup.sats_usd_price", return_value=0.001),
        patch("routstr.upstream.auto_topup.logger") as log,
    ):
        await _check_and_topup(_ppq_row())

    assert any(
        "claim was already released" in call.args[0]
        for call in log.critical.call_args_list
    )


@pytest.mark.parametrize(
    ("provider_type", "settings", "expected"),
    [
        ("ppqai", {"auto_topup": False, "topup_threshold": -1}, None),
        ("ppqai", {"auto_topup": True, "topup_threshold": 5, "topup_amount_limit": 10}, None),
        (
            "ppqai",
            {"auto_topup": True, "topup_threshold": None, "topup_amount_limit": 10},
            "threshold",
        ),
        (
            "ppqai",
            {"auto_topup": True, "topup_threshold": 5, "topup_amount_limit": 0.5},
            "whole number",
        ),
        (
            "ppqai",
            {"auto_topup": True, "topup_threshold": 5, "topup_amount_limit": 5000},
            "between",
        ),
        (
            "ppqai",
            {"auto_topup": True, "topup_threshold": True, "topup_amount_limit": 10},
            "threshold",
        ),
        (
            "routstr",
            {"auto_topup": True, "topup_threshold": 5, "topup_amount_limit": 10},
            "mint URL",
        ),
        (
            "routstr",
            {
                "auto_topup": True,
                "topup_threshold": 5,
                "topup_amount_limit": 10,
                "topup_mint_url": "https://mint.test",
            },
            None,
        ),
    ],
)
def test_auto_topup_settings_validation(
    provider_type: str, settings: dict, expected: str | None
) -> None:
    problem = validate_auto_topup_settings(provider_type, settings)
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and expected in problem


def test_auto_topup_settings_validation_survives_huge_json_integers() -> None:
    # json.loads happily produces integers past float range; float() raises
    # OverflowError there instead of returning inf.
    problem = validate_auto_topup_settings(
        "ppqai",
        {"auto_topup": True, "topup_threshold": 10**400, "topup_amount_limit": 10},
    )
    assert problem is not None and "threshold" in problem
