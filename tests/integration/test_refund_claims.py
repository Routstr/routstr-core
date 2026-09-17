"""Refund claim lifecycle against a real SQLite database.

Covers the guarantees the ``refunds`` table exists to provide: one open claim
per key, a persisted melt quote before the melt is dispatched, and a
reconciler that never restores a balance whose payout may have settled.
"""

import time
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlmodel import select

from routstr import refund
from routstr.balance import RefundRequest, refund_wallet_endpoint
from routstr.core.db import (
    ApiKey,
    AsyncSession,
    CashuTransaction,
    Refund,
    store_cashu_transaction_with_retry,
    total_user_liability,
)
from routstr.payment.lnurl import LNURLError, MeltOutcomeAmbiguousError

KEY_HASH = "refundclaimkey"
ADDRESS = "user@ln.example.com"
BALANCE_MSATS = 5_000_000


async def _seed_key(
    session: AsyncSession, *, balance: int = BALANCE_MSATS, address: str | None = None
) -> ApiKey:
    key = ApiKey(hashed_key=KEY_HASH)
    key.balance = balance
    key.reserved_balance = 0
    key.refund_currency = "sat"
    key.refund_address = address
    key.total_spent = 0
    key.total_requests = 0
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key


async def _load_key(session: AsyncSession) -> ApiKey:
    key = await session.get(ApiKey, KEY_HASH)
    assert key is not None
    await session.refresh(key)
    return key


async def _load_refund(session: AsyncSession, refund_id: str) -> Refund:
    row = await session.get(Refund, refund_id)
    assert row is not None
    await session.refresh(row)
    return row


async def _age_claim(session: AsyncSession, refund_id: str, seconds: int) -> None:
    row = await _load_refund(session, refund_id)
    row.claimed_at = int(time.time()) - seconds
    row.created_at = int(time.time()) - seconds
    row.updated_at = int(time.time()) - seconds
    session.add(row)
    await session.commit()


@pytest.fixture
def short_timeout() -> Any:
    with patch.object(refund.settings, "refund_claim_timeout_seconds", 300):
        yield


# --- exclusivity -----------------------------------------------------------


@pytest.mark.asyncio
async def test_second_claim_on_open_key_is_rejected(
    integration_session: AsyncSession,
) -> None:
    key = await _seed_key(integration_session)
    first = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    key = await _load_key(integration_session)
    assert key.balance == 0

    with pytest.raises(HTTPException) as exc_info:
        await refund.open_claim(
            integration_session, key, method="cashu", destination=None
        )
    detail = exc_info.value.detail
    assert exc_info.value.status_code == 409
    assert isinstance(detail, dict)
    assert detail["error"]["code"] == "refund_in_progress"

    rows = (await integration_session.exec(select(Refund))).all()
    assert [row.id for row in rows] == [first.id]
    assert (await _load_key(integration_session)).balance == 0


@pytest.mark.asyncio
async def test_claim_from_second_session_hits_the_index(
    integration_engine: Any, integration_session: AsyncSession
) -> None:
    key = await _seed_key(integration_session)
    await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )

    async with AsyncSession(integration_engine, expire_on_commit=False) as other:
        other_key = await _load_key(other)
        with pytest.raises(HTTPException) as exc_info:
            await refund.open_claim(
                other, other_key, method="lightning", destination=ADDRESS
            )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_claim_rejects_stale_balance_snapshot(
    integration_session: AsyncSession,
) -> None:
    key = await _seed_key(integration_session)
    integration_session.expunge(key)  # a stale, detached snapshot
    key.balance = BALANCE_MSATS + 1
    with pytest.raises(HTTPException) as exc_info:
        await refund.open_claim(
            integration_session, key, method="cashu", destination=None
        )
    assert exc_info.value.status_code == 409
    assert (await integration_session.exec(select(Refund))).all() == []
    assert (await _load_key(integration_session)).balance == BALANCE_MSATS


@pytest.mark.asyncio
async def test_retry_after_failed_claim_pays_once(
    integration_session: AsyncSession,
) -> None:
    key = await _seed_key(integration_session)
    first = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    assert await refund.release(integration_session, first)
    key = await _load_key(integration_session)
    assert key.balance == BALANCE_MSATS

    second = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    assert second.id != first.id
    assert (await _load_key(integration_session)).balance == 0


@pytest.mark.asyncio
async def test_release_after_settle_is_a_noop(
    integration_session: AsyncSession,
) -> None:
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    assert await refund.settle(integration_session, claim, quote_id="q1")
    assert not await refund.release(integration_session, claim)
    assert (await _load_key(integration_session)).balance == 0
    row = await _load_refund(integration_session, claim.id)
    assert row.status == "paid"
    assert row.claimed_at is None


# --- execute ---------------------------------------------------------------


def _lnurl_stub(
    outcome: BaseException | None = None,
    *,
    quoted: bool = True,
) -> Callable[..., Awaitable[int]]:
    async def send(
        amount: int,
        unit: str,
        mint: str,
        address: str,
        *,
        on_melt_quote: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> int:
        if quoted and on_melt_quote is not None:
            await on_melt_quote("quote-123", mint)
        if outcome is not None:
            raise outcome
        return amount

    return send


@pytest.mark.asyncio
async def test_execute_persists_quote_before_melt_and_settles(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    seen: list[str | None] = []

    async def send(*args: Any, on_melt_quote: Any = None, **kwargs: Any) -> int:
        await on_melt_quote("quote-123", claim.mint_url)
        row = await _load_refund(integration_session, claim.id)
        seen.append(row.quote_id)
        return 5000

    with patch("routstr.refund.send_to_lnurl", send):
        body = await refund.execute(integration_session, claim)

    assert seen == ["quote-123"], "quote must be on disk before the melt runs"
    assert body["status"] == "paid"
    assert body["recipient"] == ADDRESS
    assert body["sats"] == "5000"
    row = await _load_refund(integration_session, claim.id)
    assert (row.status, row.quote_id, row.claimed_at) == ("paid", "quote-123", None)


@pytest.mark.asyncio
async def test_execute_ambiguous_holds_claim_with_quote(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    with patch(
        "routstr.refund.send_to_lnurl", _lnurl_stub(MeltOutcomeAmbiguousError("?"))
    ):
        with pytest.raises(HTTPException) as exc_info:
            await refund.execute(integration_session, claim)

    assert exc_info.value.status_code == 502
    row = await _load_refund(integration_session, claim.id)
    assert (row.status, row.quote_id, row.claimed_at) == (
        "ambiguous",
        "quote-123",
        None,
    )
    assert (await _load_key(integration_session)).balance == 0


@pytest.mark.asyncio
async def test_execute_clean_failure_restores_balance(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    with patch(
        "routstr.refund.send_to_lnurl",
        _lnurl_stub(LNURLError("limits"), quoted=False),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await refund.execute(integration_session, claim)

    assert exc_info.value.status_code == 500
    row = await _load_refund(integration_session, claim.id)
    assert row.status == "failed"
    assert (await _load_key(integration_session)).balance == BALANCE_MSATS


@pytest.mark.asyncio
async def test_execute_failure_after_quote_withholds_balance(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """The mint may have paid the quote, so a later local failure must not restore."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    with patch("routstr.refund.send_to_lnurl", _lnurl_stub(RuntimeError("local"))):
        with pytest.raises(HTTPException) as exc_info:
            await refund.execute(integration_session, claim)

    assert exc_info.value.status_code == 502
    row = await _load_refund(integration_session, claim.id)
    assert (row.status, row.quote_id) == ("ambiguous", "quote-123")
    assert (await _load_key(integration_session)).balance == 0


@pytest.mark.asyncio
async def test_execute_records_mint_that_issued_the_quote(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """Mint fallback must leave reconciliation pointed at the issuing mint."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    fallback_mint = "https://fallback.mint.example"

    async def send(*args: Any, on_melt_quote: Any = None, **kwargs: Any) -> int:
        await on_melt_quote("quote-fallback", fallback_mint)
        raise MeltOutcomeAmbiguousError("unknown")

    with patch("routstr.refund.send_to_lnurl", send):
        with pytest.raises(MeltOutcomeAmbiguousError):
            await refund._pay_lightning(integration_session, claim)

    row = await _load_refund(integration_session, claim.id)
    assert (row.mint_url, row.quote_id, row.status) == (
        fallback_mint,
        "quote-fallback",
        "ambiguous",
    )


@pytest.mark.asyncio
async def test_execute_aborts_melt_when_claim_was_released(
    integration_engine: Any, integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """A reconciler that released the claim first must stop the melt."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    melted = False

    async def send(*args: Any, on_melt_quote: Any = None, **kwargs: Any) -> int:
        nonlocal melted
        async with AsyncSession(integration_engine, expire_on_commit=False) as other:
            await refund.release(other, await _load_refund(other, claim.id))
        await on_melt_quote("quote-123", claim.mint_url)
        melted = True
        return 5000

    with patch("routstr.refund.send_to_lnurl", send):
        with pytest.raises(HTTPException):
            await refund.execute(integration_session, claim)

    assert melted is False
    assert (await _load_key(integration_session)).balance == BALANCE_MSATS


# --- reconciler ------------------------------------------------------------


async def _open_ambiguous(session: AsyncSession, quote_id: str | None) -> Refund:
    key = await _seed_key(session)
    claim = await refund.open_claim(
        session, key, method="lightning", destination=ADDRESS
    )
    await refund.hold(session, claim, quote_id)
    return claim


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mint_status", "expected_status", "expected_balance"),
    [
        ("paid", "paid", 0),
        ("unpaid", "failed", BALANCE_MSATS),
        ("pending", "ambiguous", 0),
        ("unknown", "ambiguous", 0),
    ],
)
async def test_reconcile_ambiguous_claims(
    integration_session: AsyncSession,
    patched_db_engine: None,
    short_timeout: None,
    mint_status: str,
    expected_status: str,
    expected_balance: int,
) -> None:
    claim = await _open_ambiguous(integration_session, "quote-123")
    await _age_claim(integration_session, claim.id, 600)
    with patch(
        "routstr.refund.check_bolt11_payment_status",
        AsyncMock(return_value=mint_status),
    ) as check:
        await refund.reconcile_once()

    check.assert_awaited_once_with(claim.mint_url, "sat", "quote-123")
    row = await _load_refund(integration_session, claim.id)
    assert row.status == expected_status
    assert (await _load_key(integration_session)).balance == expected_balance


@pytest.mark.asyncio
async def test_reconcile_credits_balance_once_across_passes(
    integration_session: AsyncSession, patched_db_engine: None, short_timeout: None
) -> None:
    claim = await _open_ambiguous(integration_session, "quote-123")
    await _age_claim(integration_session, claim.id, 600)
    with patch(
        "routstr.refund.check_bolt11_payment_status", AsyncMock(return_value="unpaid")
    ):
        await refund.reconcile_once()
        await refund.reconcile_once()
    assert (await _load_key(integration_session)).balance == BALANCE_MSATS


@pytest.mark.asyncio
async def test_reconcile_waits_before_trusting_recent_unpaid(
    integration_session: AsyncSession, patched_db_engine: None, short_timeout: None
) -> None:
    """A just-dispatched melt can report unpaid before it turns pending, so an
    unpaid quote is only final once the claim has been quiet for a timeout."""
    claim = await _open_ambiguous(integration_session, "quote-123")
    with patch(
        "routstr.refund.check_bolt11_payment_status", AsyncMock(return_value="unpaid")
    ):
        await refund.reconcile_once()
    row = await _load_refund(integration_session, claim.id)
    assert row.status == "ambiguous"
    assert (await _load_key(integration_session)).balance == 0

    await _age_claim(integration_session, claim.id, 600)
    with patch(
        "routstr.refund.check_bolt11_payment_status", AsyncMock(return_value="unpaid")
    ):
        await refund.reconcile_once()
    row = await _load_refund(integration_session, claim.id)
    assert row.status == "failed"
    assert (await _load_key(integration_session)).balance == BALANCE_MSATS


@pytest.mark.asyncio
async def test_reconcile_keeps_claim_that_gained_quote_mid_pass(
    integration_session: AsyncSession, patched_db_engine: None, short_timeout: None
) -> None:
    """The payout records its quote between the reconciler's read and its
    release: the stale ``quote_id is None`` must not restore the balance."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    await _age_claim(integration_session, claim.id, 600)

    real_lease = refund._lease

    async def lease_then_quote(refund_id: str, now: int, cutoff: int) -> bool:
        leased = await real_lease(refund_id, now, cutoff)
        await refund.record_quote(claim, "late-quote", claim.mint_url)
        return leased

    with patch("routstr.refund._lease", lease_then_quote):
        await refund.reconcile_once()

    row = await _load_refund(integration_session, claim.id)
    assert row.status == "pending"
    assert row.quote_id == "late-quote"
    assert (await _load_key(integration_session)).balance == 0


@pytest.mark.asyncio
async def test_reconcile_leaves_fresh_pending_claim_alone(
    integration_session: AsyncSession, patched_db_engine: None, short_timeout: None
) -> None:
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    with patch("routstr.refund.check_bolt11_payment_status", AsyncMock()) as check:
        await refund.reconcile_once()
    check.assert_not_awaited()
    row = await _load_refund(integration_session, claim.id)
    assert row.status == "pending"
    assert row.claimed_at is not None
    assert (await _load_key(integration_session)).balance == 0


@pytest.mark.asyncio
async def test_reconcile_releases_expired_claim_without_quote(
    integration_session: AsyncSession, patched_db_engine: None, short_timeout: None
) -> None:
    """No quote on disk means the mint was never asked to pay."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    await _age_claim(integration_session, claim.id, 600)
    with patch("routstr.refund.check_bolt11_payment_status", AsyncMock()) as check:
        await refund.reconcile_once()
    check.assert_not_awaited()
    assert (await _load_refund(integration_session, claim.id)).status == "failed"
    assert (await _load_key(integration_session)).balance == BALANCE_MSATS


@pytest.mark.asyncio
async def test_reconcile_queries_mint_for_crashed_claim_with_quote(
    integration_session: AsyncSession, patched_db_engine: None, short_timeout: None
) -> None:
    """Crash after the quote was persisted: the mint decides, not the timeout."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    await refund.record_quote(claim, "quote-crash", claim.mint_url)
    await _age_claim(integration_session, claim.id, 600)
    with patch(
        "routstr.refund.check_bolt11_payment_status", AsyncMock(return_value="paid")
    ) as check:
        await refund.reconcile_once()
    check.assert_awaited_once_with(claim.mint_url, "sat", "quote-crash")
    assert (await _load_refund(integration_session, claim.id)).status == "paid"
    assert (await _load_key(integration_session)).balance == 0


@pytest.mark.asyncio
async def test_reconcile_marks_expired_cashu_claim_stuck(
    integration_session: AsyncSession, patched_db_engine: None, short_timeout: None
) -> None:
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="cashu", destination=None
    )
    await _age_claim(integration_session, claim.id, 600)
    with patch("routstr.refund.logger") as log:
        await refund.reconcile_once()
        await refund.reconcile_once()
    assert log.critical.call_count == 1
    row = await _load_refund(integration_session, claim.id)
    assert (row.status, row.claimed_at) == ("stuck", None)
    assert (await _load_key(integration_session)).balance == 0
    # A stuck claim is closed, so the key is not permanently locked out.
    key = await _load_key(integration_session)
    key.balance = 1000
    integration_session.add(key)
    await integration_session.commit()
    await refund.open_claim(integration_session, key, method="cashu", destination=None)


@pytest.mark.asyncio
async def test_reconcile_survives_one_failing_row(
    integration_session: AsyncSession, patched_db_engine: None, short_timeout: None
) -> None:
    claim = await _open_ambiguous(integration_session, "quote-123")
    with patch(
        "routstr.refund.check_bolt11_payment_status",
        AsyncMock(side_effect=RuntimeError("mint down")),
    ):
        await refund.reconcile_once()
    row = await _load_refund(integration_session, claim.id)
    assert row.status == "ambiguous"
    assert row.claimed_at is not None, "lease is kept until the next pass"


# --- endpoint --------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_uses_requested_address_over_persisted(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    await _seed_key(integration_session, address="stored@ln.example.com")
    send = AsyncMock(side_effect=_lnurl_stub())
    with (
        patch("routstr.refund.get_lnurl_data", AsyncMock()) as resolve,
        patch("routstr.refund.send_to_lnurl", send),
    ):
        body = await refund_wallet_endpoint(
            refund_request=RefundRequest(lightning_address=ADDRESS),
            authorization=f"Bearer sk-{KEY_HASH}",
            x_cashu=None,
            session=integration_session,
        )
    resolve.assert_awaited_once_with(ADDRESS)
    assert isinstance(body, dict)
    assert body["recipient"] == ADDRESS
    assert send.await_args is not None
    assert send.await_args.args[3] == ADDRESS
    assert (await _load_key(integration_session)).balance == 0


@pytest.mark.asyncio
async def test_endpoint_rejects_bad_address_without_debit(
    integration_session: AsyncSession,
) -> None:
    await _seed_key(integration_session)
    with patch(
        "routstr.refund.get_lnurl_data", AsyncMock(side_effect=LNURLError("nope"))
    ):
        with pytest.raises(HTTPException) as exc_info:
            await refund_wallet_endpoint(
                refund_request=RefundRequest(lightning_address="bad@example"),
                authorization=f"Bearer sk-{KEY_HASH}",
                x_cashu=None,
                session=integration_session,
            )
    assert exc_info.value.status_code == 400
    assert (await integration_session.exec(select(Refund))).all() == []
    assert (await _load_key(integration_session)).balance == BALANCE_MSATS


@pytest.mark.asyncio
async def test_endpoint_replays_paid_lightning_refund_on_empty_balance(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    await _seed_key(integration_session, address=ADDRESS)
    with (
        patch("routstr.refund.get_lnurl_data", AsyncMock()),
        patch("routstr.refund.send_to_lnurl", _lnurl_stub()),
    ):
        first = await refund_wallet_endpoint(
            authorization=f"Bearer sk-{KEY_HASH}",
            x_cashu=None,
            session=integration_session,
        )
    second = await refund_wallet_endpoint(
        authorization=f"Bearer sk-{KEY_HASH}",
        x_cashu=None,
        session=integration_session,
    )
    assert isinstance(first, dict) and isinstance(second, dict)
    assert second["refund_id"] == first["refund_id"]
    assert second["status"] == "paid"


@pytest.mark.asyncio
async def test_endpoint_refund_while_ambiguous_returns_409(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    await _open_ambiguous(integration_session, "quote-123")
    key = await _load_key(integration_session)
    key.balance = 2_000_000  # topped up while the melt is unresolved
    integration_session.add(key)
    await integration_session.commit()

    send = AsyncMock()
    with (
        patch("routstr.refund.get_lnurl_data", AsyncMock()),
        patch("routstr.refund.send_to_lnurl", send),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await refund_wallet_endpoint(
                refund_request=RefundRequest(lightning_address=ADDRESS),
                authorization=f"Bearer sk-{KEY_HASH}",
                x_cashu=None,
                session=integration_session,
            )
    assert exc_info.value.status_code == 409
    send.assert_not_awaited()
    assert (await _load_key(integration_session)).balance == 2_000_000


@pytest.mark.asyncio
async def test_endpoint_zero_balance_with_open_claim_returns_409(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """A prior refund debited the balance and is still settling: the retry must
    report refund_in_progress (409), not "no balance to refund" (400)."""
    await _open_ambiguous(integration_session, "quote-123")
    assert (await _load_key(integration_session)).balance == 0

    send = AsyncMock()
    with (
        patch("routstr.refund.get_lnurl_data", AsyncMock()),
        patch("routstr.refund.send_to_lnurl", send),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await refund_wallet_endpoint(
                authorization=f"Bearer sk-{KEY_HASH}",
                x_cashu=None,
                session=integration_session,
            )
    assert exc_info.value.status_code == 409
    assert isinstance(exc_info.value.detail, dict)
    assert exc_info.value.detail["error"]["code"] == "refund_in_progress"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cashu_token_survives_failed_ledger_write(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """The token is issued once the mint signs it; a failed cashu_transactions
    insert must neither fail the request nor release the balance, and a retry
    must replay the token from the claim row."""
    await _seed_key(integration_session)
    with (
        patch("routstr.refund.send_token", AsyncMock(return_value="cashuAtoken")),
        patch("routstr.refund.token_mint_url", lambda token, mint: mint),
        patch(
            "routstr.refund.store_cashu_transaction",
            AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        first = await refund_wallet_endpoint(
            authorization=f"Bearer sk-{KEY_HASH}",
            x_cashu=None,
            session=integration_session,
        )
    assert isinstance(first, dict)
    assert (first["token"], first["status"]) == ("cashuAtoken", "paid")
    assert (await _load_key(integration_session)).balance == 0

    second = await refund_wallet_endpoint(
        authorization=f"Bearer sk-{KEY_HASH}",
        x_cashu=None,
        session=integration_session,
    )
    assert isinstance(second, dict)
    assert second["refund_id"] == first["refund_id"]
    assert second["token"] == "cashuAtoken"


async def _topup(session: AsyncSession, amount: int = BALANCE_MSATS) -> None:
    key = await _load_key(session)
    key.balance = amount
    session.add(key)
    await session.commit()


async def _refund_cashu(
    session: AsyncSession, token: str, *, ledger: bool
) -> dict[str, str]:
    store = (
        AsyncMock(side_effect=RuntimeError("db down"))
        if not ledger
        else store_cashu_transaction_with_retry
    )
    with (
        patch("routstr.refund.send_token", AsyncMock(return_value=token)),
        patch("routstr.refund.token_mint_url", lambda t, mint: mint),
        patch("routstr.refund.store_cashu_transaction", store),
    ):
        body = await refund_wallet_endpoint(
            authorization=f"Bearer sk-{KEY_HASH}",
            x_cashu=None,
            session=session,
        )
    assert isinstance(body, dict)
    return body


@pytest.mark.asyncio
async def test_replay_prefers_the_token_of_the_newest_paid_claim(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """An older ledger row must not answer for a newer claim whose write failed."""
    await _seed_key(integration_session)
    await _refund_cashu(integration_session, "cashuAold", ledger=True)
    await _topup(integration_session)
    newest = await _refund_cashu(integration_session, "cashuAnew", ledger=False)

    replay = await refund_wallet_endpoint(
        authorization=f"Bearer sk-{KEY_HASH}",
        x_cashu=None,
        session=integration_session,
    )
    assert isinstance(replay, dict)
    assert replay["token"] == "cashuAnew"
    assert replay["refund_id"] == newest["refund_id"]


@pytest.mark.asyncio
async def test_swept_older_ledger_row_does_not_reject_the_newest_claim(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    await _seed_key(integration_session)
    await _refund_cashu(integration_session, "cashuAold", ledger=True)
    await _topup(integration_session)
    await _refund_cashu(integration_session, "cashuAnew", ledger=False)

    result = await integration_session.exec(
        select(CashuTransaction).where(CashuTransaction.token == "cashuAold")
    )
    old_tx = result.one()
    old_tx.swept = True
    integration_session.add(old_tx)
    await integration_session.commit()

    replay = await refund_wallet_endpoint(
        authorization=f"Bearer sk-{KEY_HASH}",
        x_cashu=None,
        session=integration_session,
    )
    assert isinstance(replay, dict)
    assert replay["token"] == "cashuAnew"


@pytest.mark.asyncio
async def test_open_claim_is_reported_over_an_older_paid_claim(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """A paid claim from a previous cycle must not be replayed as the outcome
    of the claim that is still settling."""
    await _seed_key(integration_session, address=ADDRESS)
    with (
        patch("routstr.refund.get_lnurl_data", AsyncMock()),
        patch("routstr.refund.send_to_lnurl", _lnurl_stub()),
    ):
        paid = await refund_wallet_endpoint(
            authorization=f"Bearer sk-{KEY_HASH}",
            x_cashu=None,
            session=integration_session,
        )
    assert isinstance(paid, dict)
    await _topup(integration_session)
    key = await _load_key(integration_session)
    open_claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )
    await refund.hold(integration_session, open_claim, "quote-open")

    validate = AsyncMock()
    with patch("routstr.refund.get_lnurl_data", validate):
        with pytest.raises(HTTPException) as exc_info:
            await refund_wallet_endpoint(
                refund_request=RefundRequest(lightning_address=ADDRESS),
                authorization=f"Bearer sk-{KEY_HASH}",
                x_cashu=None,
                session=integration_session,
            )

    assert exc_info.value.status_code == 409
    assert isinstance(exc_info.value.detail, dict)
    error = exc_info.value.detail["error"]
    assert (error["refund_id"], error["status"]) == (open_claim.id, "ambiguous")
    # A drained key must not be able to drive outbound destination lookups.
    validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_stuck_claim_is_reported_instead_of_no_balance(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="cashu", destination=None
    )
    await refund._close(integration_session, claim, status="stuck")
    await integration_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await refund_wallet_endpoint(
            authorization=f"Bearer sk-{KEY_HASH}",
            x_cashu=None,
            session=integration_session,
        )
    assert exc_info.value.status_code == 409
    assert isinstance(exc_info.value.detail, dict)
    error = exc_info.value.detail["error"]
    assert (error["code"], error["refund_id"]) == ("refund_unresolved", claim.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "counted"),
    [
        ("pending", True),
        ("ambiguous", True),
        ("stuck", True),
        ("paid", False),
        ("failed", False),
    ],
)
async def test_liability_covers_claims_until_they_resolve(
    integration_session: AsyncSession,
    patched_db_engine: None,
    status: str,
    counted: bool,
) -> None:
    """Money in flight is still owed to the customer; an owner payout that read
    only key balances could spend its backing."""
    key = await _seed_key(integration_session)
    before = await total_user_liability(integration_session)
    assert before == BALANCE_MSATS

    claim = await refund.open_claim(
        integration_session, key, method="cashu", destination=None
    )
    assert await total_user_liability(integration_session) == BALANCE_MSATS

    await refund._close(integration_session, claim, status=status)
    await integration_session.commit()
    expected = BALANCE_MSATS if counted else 0
    assert await total_user_liability(integration_session) == expected


@pytest.mark.asyncio
async def test_unreachable_destination_is_a_client_error() -> None:
    with patch(
        "routstr.refund.get_lnurl_data",
        AsyncMock(side_effect=httpx.ConnectError("All connection attempts failed")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await refund.validate_lightning_destination(ADDRESS)
    assert exc_info.value.status_code == 400
