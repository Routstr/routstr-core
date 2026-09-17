"""Refund payouts that finish after the claim row stopped cooperating.

Each test pins one guarantee of the claim table that the happy path cannot
exercise: the payout side has authoritative knowledge of what the mint did,
and the claim row must end up agreeing with it.
"""

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from routstr import refund
from routstr.balance import RefundRequest, refund_wallet_endpoint
from routstr.core.db import ApiKey, AsyncSession, Refund, total_user_liability
from routstr.payment.lnurl import MeltUnpaidError

KEY_HASH = "refundrecoverykey"
ADDRESS = "user@ln.example.com"
BALANCE_MSATS = 5_000_000


async def _seed_key(session: AsyncSession, *, address: str | None = None) -> ApiKey:
    key = ApiKey(hashed_key=KEY_HASH)
    key.balance = BALANCE_MSATS
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


def _cashu_payout(token: str, send: Any | None = None) -> Any:
    return (
        patch("routstr.refund.send_token", send or AsyncMock(return_value=token)),
        patch("routstr.refund.token_mint_url", lambda t, mint: mint),
        patch("routstr.refund.store_cashu_transaction", AsyncMock()),
    )


# --- cashu token issued after the reconciler gave up -----------------------


@pytest.mark.asyncio
async def test_cashu_token_issued_after_reconciler_marked_claim_stuck_settles_it(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """A token in the customer's hands must leave its claim ``paid``: a row
    left ``stuck`` keeps the amount in liability forever and tells the operator
    to reconcile a payout that already happened."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="cashu", destination=None
    )

    async def slow_send_token(amount: int, unit: str, mint_url: str) -> str:
        # The lease lapses while the mint is still working.
        row = await _load_refund(integration_session, claim.id)
        row.claimed_at = (row.claimed_at or 0) - 10_000
        integration_session.add(row)
        await integration_session.commit()
        await refund.reconcile_once()
        assert (await _load_refund(integration_session, claim.id)).status == "stuck"
        return "cashuAlate"

    send, mint, store = _cashu_payout("cashuAlate", slow_send_token)
    with send, mint, store:
        body = await refund.execute(integration_session, claim)

    assert (body["token"], body["status"]) == ("cashuAlate", "paid")
    row = await _load_refund(integration_session, claim.id)
    assert (row.status, row.token, row.claimed_at) == ("paid", "cashuAlate", None)
    assert await total_user_liability(integration_session) == 0


@pytest.mark.asyncio
async def test_cashu_payout_renews_lease_before_asking_the_mint(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """The reconciler leaves a claim alone while its lease is fresh, so the
    payout renews it right before the slow step."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="cashu", destination=None
    )
    row = await _load_refund(integration_session, claim.id)
    row.claimed_at = (row.claimed_at or 0) - 10_000
    integration_session.add(row)
    await integration_session.commit()

    async def send_token(amount: int, unit: str, mint_url: str) -> str:
        await refund.reconcile_once()
        return "cashuAfresh"

    send, mint, store = _cashu_payout("cashuAfresh", send_token)
    with send, mint, store, patch("routstr.refund.logger") as log:
        await refund.execute(integration_session, claim)

    log.critical.assert_not_called()
    assert (await _load_refund(integration_session, claim.id)).status == "paid"


# --- cashu token issued, claim write failed --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["execute", "autoflush", "commit"])
async def test_cashu_claim_write_failure_after_token_creation_withholds_balance(
    integration_session: AsyncSession,
    patched_db_engine: None,
    integration_engine: Any,
    failure_point: str,
) -> None:
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="cashu", destination=None
    )
    claim_id = claim.id
    fired = False
    armed = False

    def fail_once(*args: Any) -> None:
        nonlocal fired
        statement = args[2] if failure_point != "commit" else "COMMIT"
        if (
            armed
            and not fired
            and (statement.startswith("UPDATE refunds") or statement == "COMMIT")
        ):
            fired = True
            raise OperationalError(statement, {}, Exception("database is locked"))

    async def send_token(amount: int, unit: str, mint_url: str) -> str:
        nonlocal armed
        if failure_point == "autoflush":
            # A pending ORM write makes SQLAlchemy invalidate the transaction
            # and expire attached objects when the actual SQL execution fails.
            claim.updated_at -= 1
        armed = True
        return "cashuAstranded"

    event_name = "commit" if failure_point == "commit" else "before_cursor_execute"
    event.listen(integration_engine.sync_engine, event_name, fail_once)
    send, _, store = _cashu_payout("cashuAstranded", send_token)
    try:
        with (
            send,
            store,
            patch(
                "routstr.refund.token_mint_url", return_value="https://fallback.mint"
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await refund.execute(integration_session, claim)
    finally:
        event.remove(integration_engine.sync_engine, event_name, fail_once)

    assert fired
    assert exc_info.value.status_code == 502
    row = await _load_refund(integration_session, claim_id)
    assert (row.status, row.token, row.mint_url) == (
        "ambiguous",
        "cashuAstranded",
        "https://fallback.mint",
    )
    assert (await _load_key(integration_session)).balance == 0
    assert await total_user_liability(integration_session) == BALANCE_MSATS

    await refund.reconcile_once()
    row = await _load_refund(integration_session, claim_id)
    assert (row.status, row.token) == ("paid", "cashuAstranded")
    assert await total_user_liability(integration_session) == 0
    with patch("routstr.refund.send_token", AsyncMock()) as send_again:
        replay = await refund_wallet_endpoint(
            refund_request=RefundRequest(),
            authorization=f"Bearer sk-{KEY_HASH}",
            x_cashu=None,
            session=integration_session,
        )
    assert isinstance(replay, dict)
    assert replay["token"] == "cashuAstranded"
    send_again.assert_not_awaited()
    assert (await _load_key(integration_session)).balance == 0


@pytest.mark.asyncio
async def test_reconciler_settles_held_cashu_claim_that_carries_a_token(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """A held cashu claim whose row carries the token is a completed payout;
    the reconciler closes it as paid instead of escalating it to stuck."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="cashu", destination=None
    )
    await refund.hold(integration_session, claim, None, token="cashuAheld")
    row = await _load_refund(integration_session, claim.id)
    row.claimed_at = None
    row.updated_at -= 10_000
    integration_session.add(row)
    await integration_session.commit()

    with patch("routstr.refund.logger") as log:
        await refund.reconcile_once()

    log.critical.assert_not_called()
    row = await _load_refund(integration_session, claim.id)
    assert (row.status, row.token) == ("paid", "cashuAheld")
    assert await total_user_liability(integration_session) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_before_lease", [True, False])
async def test_stale_cashu_reconciliation_preserves_newly_held_token(
    integration_session: AsyncSession,
    patched_db_engine: None,
    integration_engine: Any,
    hold_before_lease: bool,
) -> None:
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="cashu", destination=None
    )
    claim_id = claim.id
    now = int(time.time())
    cutoff = now - refund.settings.refund_claim_timeout_seconds
    claim.claimed_at = cutoff - 1
    await integration_session.commit()
    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        stale = await session.get(Refund, claim_id)
    assert stale is not None and stale.token is None

    if hold_before_lease:
        await refund.hold(integration_session, claim, None, token="cashuAheld")
    assert await refund._lease(claim_id, now, cutoff)
    real_close = refund._close

    async def hold_before_close(session: AsyncSession, row: Refund, **kw: Any) -> bool:
        if not hold_before_lease and kw.get("status") == "stuck":
            # Token arrives at the last moment, even after a potential reload.
            await real_close(
                integration_session, claim, status="ambiguous", token="cashuAheld"
            )
            await integration_session.commit()
        return await real_close(session, row, **kw)

    with patch.object(refund, "_close", hold_before_close):
        await refund._reconcile(stale, now)

    row = await _load_refund(integration_session, claim_id)
    assert (row.status, row.token) == ("paid", "cashuAheld")
    assert (await _load_key(integration_session)).balance == 0
    assert await total_user_liability(integration_session) == 0
    with patch("routstr.refund.send_token", AsyncMock()) as send_again:
        replay = await refund_wallet_endpoint(
            refund_request=RefundRequest(),
            authorization=f"Bearer sk-{KEY_HASH}",
            x_cashu=None,
            session=integration_session,
        )
    assert isinstance(replay, dict)
    assert replay["token"] == "cashuAheld"
    send_again.assert_not_awaited()


# --- mint proved the melt unpaid -------------------------------------------


@pytest.mark.asyncio
async def test_mint_confirmed_unpaid_melt_restores_balance_immediately(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """The mint answering ``unpaid`` to the melt itself is proof no payment
    happened, so the customer gets the balance back now, not after the
    reconciler timeout."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="lightning", destination=ADDRESS
    )

    async def send(*args: Any, on_melt_quote: Any = None, **kwargs: Any) -> int:
        await on_melt_quote("quote-unpaid", claim.mint_url)
        raise MeltUnpaidError("Cashu mint confirmed that the melt was unpaid")

    with patch("routstr.refund.send_to_lnurl", send):
        with pytest.raises(HTTPException) as exc_info:
            await refund.execute(integration_session, claim)

    assert exc_info.value.status_code == 503
    row = await _load_refund(integration_session, claim.id)
    assert (row.status, row.quote_id) == ("failed", "quote-unpaid")
    assert (await _load_key(integration_session)).balance == BALANCE_MSATS


# --- response reflects the persisted claim ---------------------------------


@pytest.mark.asyncio
async def test_response_status_reflects_persisted_claim(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """When ``settle`` closes nothing the response must not invent ``paid``."""
    key = await _seed_key(integration_session)
    claim = await refund.open_claim(
        integration_session, key, method="cashu", destination=None
    )

    async def send_token(amount: int, unit: str, mint_url: str) -> str:
        # Somebody closed the row as failed while the mint was working.
        await refund._close(integration_session, claim, status="failed")
        await integration_session.commit()
        return "cashuAorphan"

    send, mint, store = _cashu_payout("cashuAorphan", send_token)
    with send, mint, store:
        body = await refund.execute(integration_session, claim)

    assert body["status"] == "failed"
    assert (await _load_refund(integration_session, claim.id)).status == "failed"


@pytest.mark.asyncio
async def test_stored_refund_address_is_validated_before_debit(
    integration_session: AsyncSession, patched_db_engine: None
) -> None:
    """A bad address stored on the key is a client error, not a payout failure."""
    await _seed_key(integration_session, address="nobody@invalid.example")
    send = AsyncMock()
    with (
        patch(
            "routstr.refund.get_lnurl_data",
            AsyncMock(side_effect=refund.LNURLError("no such user")),
        ),
        patch("routstr.refund.send_to_lnurl", send),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await refund_wallet_endpoint(
                refund_request=RefundRequest(),
                authorization=f"Bearer sk-{KEY_HASH}",
                x_cashu=None,
                session=integration_session,
            )

    assert exc_info.value.status_code == 400
    send.assert_not_awaited()
    assert (await _load_key(integration_session)).balance == BALANCE_MSATS
