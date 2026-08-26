"""Real-database tests for the PPQ auto top-up claim lifecycle.

These exercise the claim against actual SQL rather than mocked sessions,
because the guarantees under test are all about what the database will and
will not let two concurrent writers do.
"""

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

from routstr.core.db import CashuTransaction, create_session
from routstr.upstream.auto_topup import (
    PPQ_PHASE_CLAIMED,
    PPQ_PHASE_IN_FLIGHT,
    PPQ_PHASE_RECONCILE,
    _claim_ppq_topup,
    _ppq_payment_id,
    _ppq_payment_usd,
    _ppq_request_id,
    _ppq_spent_last_24h_usd,
    _ppq_state_id_for_provider,
    _reconcile_ppq_state,
    _record_ppq_invoice,
    _set_ppq_state_terminal,
    get_ppq_auto_topup_state,
    release_ppq_auto_topup_state,
)

pytestmark = pytest.mark.asyncio


def _row(provider_id: int = 1) -> MagicMock:
    row = MagicMock()
    row.id = provider_id
    row.api_key = "secret"
    row.provider_settings = None
    return row


async def _seed_provider(provider_id: int = 1, slug: str = "ppq") -> None:
    """Claim creation is fenced on the provider row existing; seed it."""
    from routstr.core.db import UpstreamProviderRow

    async with create_session() as session:
        session.add(
            UpstreamProviderRow(
                id=provider_id,
                slug=slug,
                provider_type="ppqai",
                base_url="https://api.ppq.ai",
                api_key="secret",
                enabled=True,
            )
        )
        await session.commit()


async def _state_row(provider_id: int = 1) -> CashuTransaction | None:
    async with create_session() as session:
        return await session.get(
            CashuTransaction, _ppq_state_id_for_provider(provider_id)
        )


async def _seed_claim(
    provider_id: int,
    phase: str,
    invoice_id: str,
    lease_expires_at: int,
    quote_id: str = "quote-1",
) -> str:
    """Seed a claim row and return its state token (the full request_id)."""
    token = _ppq_request_id(
        "operation-1", lease_expires_at, phase, invoice_id, quote_id
    )
    async with create_session() as session:
        session.add(
            CashuTransaction(
                id=_ppq_state_id_for_provider(provider_id),
                token="lnbc-invoice",
                amount=102,
                unit="sat",
                type="out",
                request_id=token,
                mint_url="https://mint.test",
                collected=False,
                source="ppq_auto_topup",
            )
        )
        await session.commit()
    return token


async def test_second_claim_is_refused_while_the_first_is_active(
    patched_db_engine: Any,
) -> None:
    await _seed_provider()
    assert await _claim_ppq_topup(_row()) is not None
    # The whole point of the claim: a concurrent cycle must not get one.
    assert await _claim_ppq_topup(_row()) is None

    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert len(rows) == 1


async def test_claim_is_reusable_once_the_previous_attempt_finished(
    patched_db_engine: Any,
) -> None:
    await _seed_provider()
    first = await _claim_ppq_topup(_row())
    assert first is not None
    assert await _set_ppq_state_terminal(_row(), first, collected=False, swept=True)

    second = await _claim_ppq_topup(_row())
    assert second is not None and second != first


async def test_settled_claim_suppresses_immediate_duplicate(
    patched_db_engine: Any,
) -> None:
    await _seed_provider()
    row = _row()
    operation_id = await _claim_ppq_topup(row)
    assert operation_id is not None
    assert await _set_ppq_state_terminal(row, operation_id, collected=True, swept=False)

    assert await _reconcile_ppq_state(row, provider=None) is True
    assert await _claim_ppq_topup(row) is None


async def test_claim_rejects_stale_provider_configuration(
    patched_db_engine: Any,
) -> None:
    await _seed_provider()
    stale = _row()
    stale.provider_settings = '{"auto_topup":true}'

    assert await _claim_ppq_topup(stale) is None


async def test_recording_the_invoice_moves_the_claim_in_flight(
    patched_db_engine: Any,
) -> None:
    await _seed_provider()
    operation_id = await _claim_ppq_topup(_row())
    assert operation_id is not None

    state = await get_ppq_auto_topup_state(1)
    assert state["phase"] == PPQ_PHASE_CLAIMED
    assert state["releasable"] is True
    assert state["invoice_id"] is None

    lease = await _record_ppq_invoice(
        _row(),
        operation_id,
        invoice="lnbc-invoice",
        invoice_id="invoice-1",
        quote_id="quote-1",
        amount=102,
        amount_usd=10,
        unit="sat",
        mint_url="https://mint.test",
    )
    assert lease > int(time.time())

    state = await get_ppq_auto_topup_state(1)
    assert state["phase"] == PPQ_PHASE_IN_FLIGHT
    assert state["invoice_id"] == "invoice-1"
    # A payment is committed to a mint, so an admin must not sweep it.
    assert state["releasable"] is False
    # The raw BOLT11 invoice must never reach the admin API.
    assert "token" not in state


async def test_release_refuses_an_in_flight_claim(patched_db_engine: Any) -> None:
    token = await _seed_claim(
        1, PPQ_PHASE_IN_FLIGHT, "invoice-1", int(time.time()) + 900
    )

    outcome = await release_ppq_auto_topup_state(1, state_token=token)

    assert outcome.released is False
    assert outcome.reason == "payment_in_flight"
    row = await _state_row()
    assert row is not None and row.swept is False


async def test_release_refuses_a_stale_state_token(patched_db_engine: Any) -> None:
    await _seed_claim(1, PPQ_PHASE_RECONCILE, "invoice-1", int(time.time()) + 900)

    outcome = await release_ppq_auto_topup_state(1, state_token="ppq:stale:token")

    assert outcome.released is False
    assert outcome.reason == "stale_state"
    row = await _state_row()
    assert row is not None and row.swept is False


async def test_release_accepts_a_reconcile_claim(patched_db_engine: Any) -> None:
    token = await _seed_claim(
        1, PPQ_PHASE_RECONCILE, "invoice-1", int(time.time()) + 900
    )

    outcome = await release_ppq_auto_topup_state(1, state_token=token)

    assert outcome.released is True
    row = await _state_row()
    assert row is not None and row.swept is True


async def test_expired_in_flight_claim_becomes_releasable(
    patched_db_engine: Any,
) -> None:
    # A worker that died mid-payment must not lock the provider forever.
    token = await _seed_claim(1, PPQ_PHASE_IN_FLIGHT, "invoice-1", int(time.time()) - 1)

    assert (await get_ppq_auto_topup_state(1))["releasable"] is True
    outcome = await release_ppq_auto_topup_state(1, state_token=token)
    assert outcome.released is True


async def test_release_reports_no_active_claim_once_swept(
    patched_db_engine: Any,
) -> None:
    token = await _seed_claim(
        1, PPQ_PHASE_RECONCILE, "invoice-1", int(time.time()) + 900
    )
    assert (await release_ppq_auto_topup_state(1, state_token=token)).released

    outcome = await release_ppq_auto_topup_state(1, state_token=token)
    assert outcome.released is False
    assert outcome.reason == "no_active_claim"


async def test_terminal_write_fails_after_the_claim_was_released(
    patched_db_engine: Any,
) -> None:
    """The symptom an admin release leaves behind for the owning worker."""
    token = await _seed_claim(
        1, PPQ_PHASE_RECONCILE, "invoice-1", int(time.time()) + 900
    )
    assert (await release_ppq_auto_topup_state(1, state_token=token)).released

    assert (
        await _set_ppq_state_terminal(
            _row(), "operation-1", collected=True, swept=False
        )
        is False
    )


async def test_ppq_claim_rows_are_excluded_from_the_admin_transaction_list(
    patched_db_engine: Any,
) -> None:
    from routstr.core.admin import get_transactions_api

    await _seed_provider()
    await _claim_ppq_topup(_row())
    async with create_session() as session:
        session.add(
            CashuTransaction(
                id="real-transaction",
                token="cashuAreal",
                amount=50,
                unit="sat",
                type="out",
                source="x-cashu",
            )
        )
        await session.commit()

    result = await get_transactions_api()

    ids = {t["id"] for t in result["transactions"]}  # type: ignore[index,union-attr]
    assert "real-transaction" in ids
    assert _ppq_state_id_for_provider(1) not in ids


async def test_ppq_payment_audit_row_is_visible_and_survives_next_claim(
    patched_db_engine: Any,
) -> None:
    from routstr.core.admin import get_transactions_api

    await _seed_provider()
    operation_id = await _claim_ppq_topup(_row())
    assert operation_id is not None
    await _record_ppq_invoice(
        _row(),
        operation_id,
        invoice="lnbc-secret-invoice",
        invoice_id="invoice-1",
        quote_id="quote-1",
        amount=102,
        amount_usd=10,
        unit="sat",
        mint_url="https://mint.test",
    )
    assert await _set_ppq_state_terminal(
        _row(), operation_id, collected=True, swept=False
    )

    result = await get_transactions_api(source="ppq_auto_topup")
    transactions = result["transactions"]
    assert len(transactions) == 1
    audit = transactions[0]
    assert audit["id"] == _ppq_payment_id(operation_id)
    assert audit["token"] == "ppq-invoice:invoice-1:usd:10"
    assert audit["collected"] is True
    assert "lnbc-secret-invoice" not in audit["token"]

    # The durable cooldown blocks an immediate duplicate, then the claim lock
    # can be reused without overwriting audit history after it expires.
    assert await _claim_ppq_topup(_row()) is None
    async with create_session() as session:
        state = await session.get(CashuTransaction, _ppq_state_id_for_provider(1))
        assert state is not None
        state.created_at = int(time.time()) - 301
        session.add(state)
        await session.commit()
    assert await _claim_ppq_topup(_row()) is not None
    async with create_session() as session:
        assert await session.get(CashuTransaction, audit["id"]) is not None


async def test_reconcile_settles_a_recorded_invoice(patched_db_engine: Any) -> None:
    from routstr.upstream.auto_topup import _reconcile_ppq_state

    await _seed_claim(1, PPQ_PHASE_IN_FLIGHT, "invoice-1", int(time.time()) + 900)
    provider = MagicMock()
    provider.check_topup_status = AsyncMock(return_value=True)

    # Still suppresses this cycle, but the claim is now finished.
    assert await _reconcile_ppq_state(_row(), provider) is True

    row = await _state_row()
    assert row is not None and row.collected is True


async def test_stale_token_from_before_a_phase_change_cannot_release(
    patched_db_engine: Any,
) -> None:
    """The blocker scenario: admin reviews `claimed`, payment turns ambiguous.

    The operation id is identical in both states, so an id-based fence would
    let the stale confirmation land. The full state token must not.
    """
    await _seed_provider()
    operation_id = await _claim_ppq_topup(_row())
    assert operation_id is not None
    reviewed = await get_ppq_auto_topup_state(1)
    assert reviewed["phase"] == PPQ_PHASE_CLAIMED

    # Worker records the invoice: same operation, new phase, proofs committed.
    await _record_ppq_invoice(
        _row(),
        operation_id,
        invoice="lnbc-invoice",
        invoice_id="invoice-1",
        quote_id="quote-1",
        amount=102,
        amount_usd=10,
        unit="sat",
        mint_url="https://mint.test",
    )

    outcome = await release_ppq_auto_topup_state(
        1, state_token=str(reviewed["state_token"])
    )
    assert outcome.released is False
    assert outcome.reason == "stale_state"
    row = await _state_row()
    assert row is not None and row.swept is False


async def test_concurrent_claims_only_one_wins(patched_db_engine: Any) -> None:
    import asyncio

    await _seed_provider()

    results = await asyncio.gather(
        *(_claim_ppq_topup(_row()) for _ in range(5)), return_exceptions=True
    )
    winners = [r for r in results if isinstance(r, str)]
    assert len(winners) == 1

    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert len(rows) == 1


async def test_reconcile_releases_claim_when_mint_reports_unpaid(
    patched_db_engine: Any,
) -> None:
    from routstr.upstream.auto_topup import _reconcile_ppq_state

    # Lease expired, PPQ never credited: only the mint's own "unpaid" answer
    # may hand the claim back.
    await _seed_claim(1, PPQ_PHASE_RECONCILE, "invoice-1", int(time.time()) - 1)
    provider = MagicMock()
    provider.check_topup_status = AsyncMock(return_value=False)

    with patch(
        "routstr.upstream.auto_topup.check_bolt11_payment_status",
        AsyncMock(return_value="unpaid"),
    ) as status:
        suppressed = await _reconcile_ppq_state(_row(), provider)

    status.assert_awaited_once_with("https://mint.test", "sat", "quote-1")
    assert suppressed is False
    row = await _state_row()
    assert row is not None and row.swept is True


async def test_reconcile_keeps_claim_when_mint_answer_is_not_final(
    patched_db_engine: Any,
) -> None:
    from routstr.upstream.auto_topup import _reconcile_ppq_state

    await _seed_claim(1, PPQ_PHASE_RECONCILE, "invoice-1", int(time.time()) - 1)
    provider = MagicMock()
    provider.check_topup_status = AsyncMock(return_value=False)

    for answer in ("paid", "pending", "unknown"):
        with patch(
            "routstr.upstream.auto_topup.check_bolt11_payment_status",
            AsyncMock(return_value=answer),
        ):
            assert await _reconcile_ppq_state(_row(), provider) is True
        row = await _state_row()
        assert row is not None and row.swept is False, answer


async def test_release_endpoint_maps_refusals_to_409(patched_db_engine: Any) -> None:
    from fastapi import HTTPException

    from routstr.core.admin import (
        ReleasePPQAutoTopupRequest,
        release_ppq_auto_topup_api,
    )

    provider_row = MagicMock()
    provider_row.provider_type = "ppqai"

    token = await _seed_claim(
        1, PPQ_PHASE_IN_FLIGHT, "invoice-1", int(time.time()) + 900
    )

    with patch(
        "routstr.core.admin._require_ppq_provider",
        AsyncMock(return_value=provider_row),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await release_ppq_auto_topup_api(
                1,
                ReleasePPQAutoTopupRequest(
                    confirmed_safe_to_retry=True, state_token=token
                ),
            )
        assert excinfo.value.status_code == 409
        assert "in flight" in excinfo.value.detail

        with pytest.raises(HTTPException) as excinfo:
            await release_ppq_auto_topup_api(
                1,
                ReleasePPQAutoTopupRequest(
                    confirmed_safe_to_retry=True, state_token="ppq:wrong"
                ),
            )
        assert excinfo.value.status_code == 409
        assert "changed since" in excinfo.value.detail


async def test_provider_delete_is_blocked_by_an_active_claim(
    patched_db_engine: Any,
) -> None:
    from fastapi import HTTPException

    from routstr.core.admin import delete_upstream_provider
    from routstr.core.db import UpstreamProviderRow

    async with create_session() as session:
        session.add(
            UpstreamProviderRow(
                id=1,
                slug="ppq",
                provider_type="ppqai",
                base_url="https://api.ppq.ai",
                api_key="secret",
                enabled=True,
            )
        )
        await session.commit()
    await _seed_claim(1, PPQ_PHASE_RECONCILE, "invoice-1", int(time.time()) + 900)

    with pytest.raises(HTTPException) as excinfo:
        await delete_upstream_provider("1")
    assert excinfo.value.status_code == 409

    # Provider must still exist.
    async with create_session() as session:
        assert await session.get(UpstreamProviderRow, 1) is not None


async def test_claim_is_refused_when_the_provider_row_is_gone(
    patched_db_engine: Any,
) -> None:
    """The worker's half of the delete race: no provider row, no claim."""
    assert await _claim_ppq_topup(_row()) is None

    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert rows == []


async def test_claim_is_refused_after_a_provider_type_change(
    patched_db_engine: Any,
) -> None:
    from routstr.core.db import UpstreamProviderRow

    await _seed_provider()
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, 1)
        assert provider is not None
        provider.provider_type = "openai"
        session.add(provider)
        await session.commit()

    assert await _claim_ppq_topup(_row()) is None


async def test_disabled_provider_with_claim_still_reconciles(
    patched_db_engine: Any,
) -> None:
    """A claim tracks committed money; eligibility must not stop reconciling."""
    from routstr.core.db import UpstreamProviderRow
    from routstr.upstream.auto_topup import _reconcile_all_ppq_claims

    await _seed_provider()
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, 1)
        assert provider is not None
        provider.enabled = False
        session.add(provider)
        await session.commit()
    await _seed_claim(1, PPQ_PHASE_RECONCILE, "invoice-1", int(time.time()) + 900)

    ppq = MagicMock()
    ppq.check_topup_status = AsyncMock(return_value=True)
    with patch(
        "routstr.upstream.auto_topup.PPQAIUpstreamProvider.from_db_row",
        return_value=ppq,
    ):
        await _reconcile_all_ppq_claims()

    row = await _state_row()
    assert row is not None and row.collected is True


async def test_claim_without_api_key_still_reconciles_via_the_mint(
    patched_db_engine: Any,
) -> None:
    from routstr.core.db import UpstreamProviderRow
    from routstr.upstream.auto_topup import _reconcile_all_ppq_claims

    await _seed_provider()
    async with create_session() as session:
        provider = await session.get(UpstreamProviderRow, 1)
        assert provider is not None
        provider.api_key = ""
        session.add(provider)
        await session.commit()
    # Lease expired, so the mint may be consulted.
    await _seed_claim(1, PPQ_PHASE_RECONCILE, "invoice-1", int(time.time()) - 1)

    with patch(
        "routstr.upstream.auto_topup.check_bolt11_payment_status",
        AsyncMock(return_value="unpaid"),
    ) as status:
        await _reconcile_all_ppq_claims()

    # No API key: PPQ was never polled, but the mint was, and its definitive
    # "unpaid" released the claim.
    status.assert_awaited_once()
    row = await _state_row()
    assert row is not None and row.swept is True


def test_ppq_payment_usd_prefers_stamped_amount() -> None:
    # Stamped rows must not move with the BTC price.
    assert _ppq_payment_usd(102, "sat", "ppq-invoice:a:usd:10", 0.5) == 10.0


def test_ppq_payment_usd_falls_back_to_current_price() -> None:
    # Rows recorded before the stamp existed convert sats at today's price.
    assert _ppq_payment_usd(2000, "sat", "ppq-invoice:legacy", 0.001) == 2.0
    assert _ppq_payment_usd(2_000_000, "msat", "ppq-invoice:legacy", 0.001) == 2.0


def test_ppq_payment_usd_survives_malformed_stamp() -> None:
    assert _ppq_payment_usd(3000, "sat", "ppq-invoice:x:usd:oops", 0.001) == 3.0


async def test_daily_spend_ignores_provably_unattempted_payments(
    patched_db_engine: Any,
) -> None:
    def _payment(
        id_: str, token: str, collected: bool, swept: bool
    ) -> CashuTransaction:
        return CashuTransaction(
            id=id_,
            token=token,
            amount=1,
            unit="sat",
            type="out",
            source="ppq_auto_topup",
            collected=collected,
            swept=swept,
        )

    async with create_session() as session:
        # Settled, in-flight, and provably-unattempted payments plus a
        # pre-stamp row: only the unattempted one must be excluded.
        session.add(_payment("pay-usd-1", "ppq-invoice:a:usd:100", True, False))
        session.add(_payment("pay-usd-2", "ppq-invoice:b:usd:50", False, False))
        session.add(_payment("pay-usd-3", "ppq-invoice:c:usd:25", False, True))
        legacy = _payment("pay-usd-4", "ppq-invoice:legacy", True, False)
        legacy.amount = 2000
        session.add(legacy)
        await session.commit()

    assert await _ppq_spent_last_24h_usd(0.001) == 152.0
