"""X-Cashu payments always reach a terminal, claimable state (issue #876).

Runs the real settlement state machine, recovery and refund endpoint against a
real SQLite database; only the mint and the upstream are faked.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlmodel import col, select, update

from routstr import wallet as wallet_module
from routstr.balance import refund_wallet_endpoint
from routstr.core.db import (
    AsyncSession,
    CashuTransaction,
    store_cashu_transaction,
    total_user_liability,
    user_liability_for_mint_and_unit,
)
from routstr.core.settings import settings
from routstr.mint import MintRateGuard
from routstr.payment import x_cashu_settlement as xs
from routstr.payment.cost_calculation import MaxCostData
from routstr.upstream import ehbp as ehbp_module
from routstr.upstream.base import X_ROUTSTR_REFUND_ERROR_HEADER, BaseUpstreamProvider
from routstr.upstream.tinfoil_trailer import TrailerResponse
from routstr.wallet import _refund_sweep_once

PAYMENT = "cashuApayment"
AMOUNT = 20
MINT = "http://mint.test"
STALE = int(xs.abandoned_after_seconds()) + 5


class FakeWallet:
    """Mints numbered refund tokens; ``gate`` holds a mint open when set."""

    def __init__(self) -> None:
        self.mints: list[int] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.fail = False

    async def send_token(
        self, amount: int, unit: str = "sat", mint_url: str | None = None
    ) -> str:
        self.mints.append(amount)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise wallet_module.NoSpendableMint("mint unavailable before selection")
        return f"cashuBrefund{len(self.mints)}x{amount}"


@pytest.fixture
def wallet(
    patched_db_engine: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeWallet]:
    monkeypatch.setattr(settings, "x_cashu_retry_unminted_refunds", True)
    fake = FakeWallet()
    with (
        patch("routstr.upstream.base.send_token", fake.send_token),
        patch("routstr.payment.x_cashu_settlement.send_token", fake.send_token),
        patch("routstr.upstream.ehbp.send_token", fake.send_token),
        patch("routstr.upstream.base.token_mint_url", lambda _t, m=None: m or MINT),
        patch("routstr.payment.x_cashu_settlement.token_mint_url", lambda _t, m: m),
        patch(
            "routstr.upstream.base.recieve_token",
            AsyncMock(return_value=(AMOUNT, "sat", MINT)),
        ),
        patch(
            "routstr.upstream.ehbp.recieve_token",
            AsyncMock(return_value=(AMOUNT, "sat", MINT)),
        ),
    ):
        yield fake


@pytest.fixture(params=["chat", "responses"])
def api(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _request(api: str, request_id: str = "req-876") -> Request:
    path = "/v1/chat/completions" if api == "chat" else "/v1/responses"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )
    request.state.request_id = request_id
    return request


Upstream = httpx.Response | BaseException | Callable[..., Awaitable[Any]]


def _client(response: Upstream) -> MagicMock:
    client = MagicMock()
    client.aclose = AsyncMock()
    if isinstance(response, httpx.Response):
        client.send = AsyncMock(return_value=response)
    else:
        client.send = AsyncMock(side_effect=response)
    return client


def _completion() -> httpx.Response:
    return httpx.Response(
        200,
        json={"model": "m", "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
    )


async def _pay(
    api: str,
    upstream: Upstream,
    cost_sats: int | None = 5,
    request_id: str = "req-876",
) -> Any:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    cost = (
        None
        if cost_sats is None
        else MaxCostData(
            base_msats=0,
            input_msats=0,
            output_msats=cost_sats * 1000,
            total_msats=cost_sats * 1000,
        )
    )
    handler = (
        provider.handle_x_cashu if api == "chat" else provider.handle_x_cashu_responses
    )
    request = _request(api, request_id)
    with (
        patch(
            "routstr.upstream.base.build_x_cashu_client",
            return_value=_client(upstream),
        ),
        patch.object(provider, "get_x_cashu_cost", AsyncMock(return_value=cost)),
    ):
        return await handler(
            request,
            PAYMENT,
            request.url.path.lstrip("/"),
            AMOUNT * 1000,
            MagicMock(),
            request_body=b"{}",
        )


async def _payment(session: AsyncSession, token: str = PAYMENT) -> CashuTransaction:
    session.expire_all()
    result = await session.exec(
        select(CashuTransaction).where(
            CashuTransaction.token == token, CashuTransaction.type == "in"
        )
    )
    payment = result.one()
    return payment


async def _refunds(session: AsyncSession, request_id: str = "req-876") -> list[int]:
    session.expire_all()
    result = await session.exec(
        select(CashuTransaction).where(
            CashuTransaction.type == "out", CashuTransaction.request_id == request_id
        )
    )
    return [row.amount for row in result.all()]


async def _claim(session: AsyncSession, token: str = PAYMENT) -> Any:
    try:
        response = await refund_wallet_endpoint(x_cashu=token, session=session)
    except HTTPException as error:
        return error
    assert isinstance(response, JSONResponse)
    return json.loads(bytes(response.body))


async def _seed(
    session: AsyncSession,
    *,
    settlement: str | None,
    age: int,
    request_id: str = "req-876",
    token: str = PAYMENT,
) -> None:
    await store_cashu_transaction(
        token=token,
        amount=AMOUNT,
        unit="sat",
        mint_url=MINT,
        typ="in",
        request_id=request_id,
        collected=True,
        created_at=int(time.time()) - age,
        settlement=settlement,
    )


async def _age(session: AsyncSession, seconds: int, token: str = PAYMENT) -> None:
    await session.exec(  # type: ignore[call-overload]
        update(CashuTransaction)
        .where(col(CashuTransaction.token) == token)
        .values(
            created_at=int(time.time()) - seconds,
            settlement_at=int(time.time()) - seconds,
            recover_after=int(time.time())
            - seconds
            + int(xs.abandoned_after_seconds()),
        )
    )
    await session.commit()


# --- live settlement ------------------------------------------------------


async def test_change_is_refunded_and_claimable_once_stored(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    response = await _pay(api, _completion(), cost_sats=5)

    assert response.headers["X-Cashu"] == "cashuBrefund1x15"
    assert wallet.mints == [15]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    for _ in range(2):
        claimed = await _claim(integration_session)
        assert claimed == {"token": "cashuBrefund1x15", "sats": "15"}


async def test_full_charge_is_terminal_not_pending(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    response = await _pay(api, _completion(), cost_sats=AMOUNT)

    assert "X-Cashu" not in response.headers
    assert wallet.mints == []
    assert (await _payment(integration_session)).settlement == xs.CHARGED
    error = await _claim(integration_session)
    assert error.status_code == 409
    assert error.detail["error"]["type"] == "token_already_spent"
    assert error.detail["error"]["code"] == "payment_fully_charged"

    # Recovery never refunds a payment that was charged.
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == []


async def test_upstream_error_refunds_in_full(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    response = await _pay(api, httpx.Response(503, json={"error": "down"}))

    assert response.headers["X-Cashu"] == f"cashuBrefund1x{AMOUNT}"
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert (await _claim(integration_session))["sats"] == str(AMOUNT)


async def test_unpriceable_response_refunds_instead_of_keeping_payment(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    response = await _pay(api, _completion(), cost_sats=None)

    assert response.status_code >= 400
    assert response.headers["X-Cashu"] == f"cashuBrefund1x{AMOUNT}"
    assert (await _payment(integration_session)).settlement == xs.REFUNDED


async def test_cancelled_request_refunds_in_full_before_reraising(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    with pytest.raises(asyncio.CancelledError):
        await _pay(api, asyncio.CancelledError())

    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert (await _claim(integration_session))["sats"] == str(AMOUNT)


async def test_cancellation_during_pending_write_keeps_refund_claimable(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_store(**kwargs: Any) -> bool:
        started.set()
        await release.wait()
        return await store_cashu_transaction(**kwargs)

    with patch("routstr.upstream.base.store_cashu_transaction", delayed_store):
        task = asyncio.create_task(_pay(api, _completion()))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert (await _claim(integration_session))["sats"] == str(AMOUNT)


async def test_cancellation_during_refund_mint_never_mints_twice(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    wallet.gate = asyncio.Event()
    task = asyncio.create_task(_pay(api, _completion(), cost_sats=5))
    await asyncio.wait_for(wallet.started.wait(), 5)

    task.cancel()
    await asyncio.sleep(0)
    wallet.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The change refund in flight completes; no second, full refund is minted.
    assert wallet.mints == [15]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert await _refunds(integration_session) == [15]


async def test_disabled_rollout_does_not_reopen_change(
    wallet: FakeWallet,
    integration_session: AsyncSession,
    api: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "x_cashu_retry_unminted_refunds", False)
    wallet.fail = True
    await _pay(api, _completion(), cost_sats=5)
    assert (await _payment(integration_session)).settlement == xs.FAILED
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 0


async def test_recovery_discards_snapshot_superseded_by_change_claim(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)
    snapshot = await _payment(integration_session)
    assert await xs.claim_refund(snapshot.request_id, 15)
    assert await xs.reopen_unminted_refund(snapshot.request_id)
    assert not await xs._refund_abandoned(snapshot)
    assert not wallet.mints
    assert await xs.recover_abandoned_payments() == 1
    assert wallet.mints == [15]


@pytest.mark.parametrize("sender", ["chat", "ehbp", "recovery"])
async def test_ambiguous_send_failure_is_not_retried(
    wallet: FakeWallet, integration_session: AsyncSession, sender: str
) -> None:
    send = AsyncMock(side_effect=OSError("failed persisting proof reservation"))
    if sender == "chat":
        with patch("routstr.upstream.base.send_token", send):
            await _pay("chat", _completion(), cost_sats=5)
    elif sender == "ehbp":
        with patch.object(ehbp_module, "send_token", send):
            response = await _ehbp_pay(
                AsyncMock(return_value=_ehbp_ok()), cost_msats=5000
            )
        # Nothing will pay the change, so the response stays withheld.
        assert response.status_code == 500
        assert b"ciphertext" not in bytes(response.body)
    else:
        await _seed(integration_session, settlement=xs.PENDING, age=STALE)
        with patch.object(xs, "send_token", send):
            assert await xs.recover_abandoned_payments() == 0
    send.assert_awaited_once()
    assert (await _payment(integration_session)).settlement == xs.FAILED
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 0
    assert not wallet.mints


async def test_failed_change_mint_is_retried_by_recovery_for_the_change(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    wallet.fail = True
    response = await _pay(api, _completion(), cost_sats=5)

    assert response.headers[X_ROUTSTR_REFUND_ERROR_HEADER] == "refund_failed"
    payment = await _payment(integration_session)
    assert payment.settlement == xs.PENDING
    assert payment.refund_amount == 15
    # A pending payment counts in full until recovery settles it.
    assert await total_user_liability(integration_session) == AMOUNT * 1000
    assert (
        await user_liability_for_mint_and_unit(integration_session, MINT, "sat")
        == AMOUNT * 1000
    )
    assert (await _claim(integration_session)).status_code == 425

    wallet.fail = False
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 1
    assert await xs.recover_abandoned_payments() == 0
    # Only the change is refunded; the inference charge stands.
    assert wallet.mints == [15, 15]
    assert await _refunds(integration_session) == [15]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert await total_user_liability(integration_session) == 0
    assert (await _claim(integration_session))["sats"] == "15"


async def test_change_refund_waits_out_mint_cooldown(
    wallet: FakeWallet,
    integration_session: AsyncSession,
    api: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "primary_mint", MINT)
    monkeypatch.setattr(settings, "cashu_mints", [MINT])
    get_wallet = AsyncMock(side_effect=AssertionError("mint contacted"))
    monkeypatch.setattr(wallet_module, "get_wallet", get_wallet)
    MintRateGuard.get(MINT).apply_cooldown(300, reason="rate_limited")

    # The real send_token finds no trusted mint out of cooldown.
    with patch("routstr.upstream.base.send_token", wallet_module.send_token):
        response = await _pay(api, _completion(), cost_sats=5)

    assert response.headers[X_ROUTSTR_REFUND_ERROR_HEADER] == "refund_failed"
    assert get_wallet.await_count == 0
    payment = await _payment(integration_session)
    assert payment.settlement == xs.PENDING
    assert payment.refund_amount == 15

    MintRateGuard.get(MINT)._cooldown_until = 0.0
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 1
    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == [15]
    assert await _refunds(integration_session) == [15]
    assert (await _claim(integration_session))["sats"] == "15"


async def test_failed_full_refund_mint_is_retried_by_recovery(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    wallet.fail = True
    response = await _pay(api, httpx.Response(503, json={"error": "down"}))

    assert response.status_code == 500
    error = json.loads(bytes(response.body))["error"]
    assert error["code"] == "refund_failed"
    assert "/v1/balance/refund" in error["message"]
    payment = await _payment(integration_session)
    assert payment.settlement == xs.PENDING
    assert (await _claim(integration_session)).status_code == 425

    wallet.fail = False
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 1
    assert wallet.mints == [AMOUNT, AMOUNT]
    assert await _refunds(integration_session) == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED


async def test_reopened_change_is_drained_after_retry_flag_is_turned_off(
    wallet: FakeWallet,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet.fail = True
    await _pay("chat", _completion(), cost_sats=5)
    payment = await _payment(integration_session)
    assert payment.settlement == xs.PENDING
    assert payment.refund_amount == 15

    monkeypatch.setattr(settings, "x_cashu_retry_unminted_refunds", False)
    wallet.fail = False
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 1
    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == [15, 15]
    assert await _refunds(integration_session) == [15]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED


@pytest.mark.parametrize("sender", ["chat", "responses", "ehbp"])
async def test_ambiguous_full_refund_failure_points_to_the_operator(
    wallet: FakeWallet, integration_session: AsyncSession, sender: str
) -> None:
    send = AsyncMock(side_effect=OSError("failed persisting proof reservation"))
    if sender == "ehbp":
        upstream_down = TrailerResponse(status_code=503, headers=[], body=b"down")
        with patch.object(ehbp_module, "send_token", send):
            response = await _ehbp_pay(AsyncMock(return_value=upstream_down), 5000)
    else:
        with patch("routstr.upstream.base.send_token", send):
            response = await _pay(sender, httpx.Response(503, json={"error": "down"}))

    assert response.status_code == 500
    error = json.loads(bytes(response.body))["error"]
    assert error["code"] == "refund_failed"
    assert "contact the operator" in error["message"]
    assert (await _payment(integration_session)).settlement == xs.FAILED
    claimed = await _claim(integration_session)
    assert claimed.status_code == 409
    assert claimed.detail["error"]["code"] == "refund_unresolved"


async def test_unstored_live_refund_token_is_terminal(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    async def out_row_fails(**kwargs: Any) -> bool:
        if kwargs["typ"] == "out":
            raise RuntimeError("database unavailable")
        return await store_cashu_transaction(**kwargs)

    with patch("routstr.upstream.base.store_cashu_transaction", out_row_fails):
        response = await _pay(api, httpx.Response(503, json={"error": "down"}))

    # The token was minted and handed out, so it must never be minted again.
    assert response.headers["X-Cashu"] == f"cashuBrefund1x{AMOUNT}"
    assert (await _payment(integration_session)).settlement == xs.FAILED
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == [AMOUNT]


async def test_refund_claim_body_names_the_payments_request_id(
    integration_client: httpx.AsyncClient, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.FAILED, age=10)

    response = await integration_client.post(
        "/v1/balance/refund", headers={"X-Cashu": PAYMENT}
    )

    # The handler sets top-level request_id to this call's id, not the payment's.
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "refund_unresolved"
    assert body["payment_request_id"] == "req-876"


async def test_in_flight_payment_answers_pending(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=10)

    error = await _claim(integration_session)
    assert error.status_code == 425
    assert error.headers["Retry-After"] == "2"


# --- recovery after a crash -------------------------------------------------


async def test_abandoned_payment_is_refunded_in_full_and_claimable(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)

    assert await xs.recover_abandoned_payments() == 1
    assert await xs.recover_abandoned_payments() == 0

    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert await _claim(integration_session) == {
        "token": f"cashuBrefund1x{AMOUNT}",
        "sats": str(AMOUNT),
    }


async def test_recent_payment_is_left_to_its_request(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=STALE - 120)

    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == []
    assert (await _payment(integration_session)).settlement == xs.PENDING


async def test_lowered_request_lifetime_keeps_the_accepted_deadline(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    # Accepted under a 1800s lifetime; the operator then lowers it to 30s.
    with patch.object(settings, "max_request_lifetime_seconds", 1800):
        await _seed(integration_session, settlement=xs.PENDING, age=200)
    with patch.object(settings, "max_request_lifetime_seconds", 30):
        assert await xs.recover_abandoned_payments() == 0
        assert (await _claim(integration_session)).status_code == 425

    assert wallet.mints == []
    assert (await _payment(integration_session)).settlement == xs.PENDING
    assert await xs.mark_charged("req-876")


async def test_raised_request_lifetime_uses_the_accepted_deadline(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    # Accepted under a 30s lifetime; the operator then raises it to 1800s.
    with patch.object(settings, "max_request_lifetime_seconds", 30):
        await _seed(integration_session, settlement=xs.PENDING, age=200)
    with patch.object(settings, "max_request_lifetime_seconds", 1800):
        assert await xs.recover_abandoned_payments() == 1

    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED


async def test_concurrent_recoveries_mint_once(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)

    results = await asyncio.gather(*(xs.recover_abandoned_payments() for _ in range(4)))

    assert sum(results) == 1
    assert wallet.mints == [AMOUNT]
    assert await _refunds(integration_session) == [AMOUNT]


async def test_live_refund_loses_to_recovery_without_minting(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    async def recovered_first(*_: Any, **__: Any) -> Any:
        await _age(integration_session, STALE, token=PAYMENT)
        assert await xs.recover_abandoned_payments() == 1
        return _completion()

    client = MagicMock()
    client.aclose = AsyncMock()
    client.send = AsyncMock(side_effect=recovered_first)
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    handler = (
        provider.handle_x_cashu if api == "chat" else provider.handle_x_cashu_responses
    )
    request = _request(api)
    cost = MaxCostData(base_msats=0, input_msats=0, output_msats=0, total_msats=5000)
    with (
        patch("routstr.upstream.base.build_x_cashu_client", return_value=client),
        patch.object(provider, "get_x_cashu_cost", AsyncMock(return_value=cost)),
    ):
        response = await handler(
            request,
            PAYMENT,
            request.url.path.lstrip("/"),
            AMOUNT * 1000,
            MagicMock(),
            request_body=b"{}",
        )

    # Recovery refunded the payment in full, so the answer is withheld.
    assert response.status_code == 503
    assert json.loads(bytes(response.body))["error"]["code"] == (
        "payment_settlement_failed"
    )
    assert wallet.mints == [AMOUNT]
    assert await _refunds(integration_session) == [AMOUNT]


def _refund_claims_fail() -> Any:
    real_transition = xs._transition

    async def claim_fails(
        request_id: str,
        from_states: Any,
        to: str,
        refund_amount: int | None = None,
    ) -> bool:
        if to == xs.REFUNDING:
            raise RuntimeError("database unavailable")
        return await real_transition(request_id, from_states, to, refund_amount)

    return patch.object(xs, "_transition", claim_fails)


async def test_failed_refund_claim_charges_instead_of_leaving_payment_pending(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    with _refund_claims_fail():
        response = await _pay(api, _completion(), cost_sats=5)

    # The answer is served as paid in full; recovery must never refund it too.
    assert response.status_code == 200
    assert response.headers["X-Routstr-Refund-Error"] == "refund_failed"
    assert wallet.mints == []
    assert (await _payment(integration_session)).settlement == xs.CHARGED
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == []


async def test_failed_refund_claim_on_upstream_error_leaves_payment_to_recovery(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    with _refund_claims_fail():
        response = await _pay(api, httpx.Response(503, json={"error": "down"}))

    # No answer was served, so recovery refunds the payment in full.
    assert response.status_code == 500
    assert json.loads(bytes(response.body))["error"]["code"] == "refund_failed"
    assert wallet.mints == []
    assert (await _payment(integration_session)).settlement == xs.PENDING
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 1
    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED


async def test_full_charge_is_withheld_when_recovery_already_refunded(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    async def recovered_first(*_: Any, **__: Any) -> Any:
        await _age(integration_session, STALE, token=PAYMENT)
        assert await xs.recover_abandoned_payments() == 1
        return _completion()

    response = await _pay(api, recovered_first, cost_sats=AMOUNT)

    # The client already has its money back, so it must not get the answer too.
    assert response.status_code == 503
    assert json.loads(bytes(response.body))["error"]["code"] == (
        "payment_settlement_failed"
    )
    assert wallet.mints == [AMOUNT]
    assert (await _claim(integration_session))["sats"] == str(AMOUNT)


async def test_full_charge_is_withheld_when_it_cannot_be_recorded(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    real_transition = xs._transition

    async def charge_fails(request_id: str, from_states: Any, to: str) -> bool:
        if to == xs.CHARGED:
            raise RuntimeError("database unavailable")
        return await real_transition(request_id, from_states, to)

    with patch.object(xs, "_transition", charge_fails):
        response = await _pay(api, _completion(), cost_sats=AMOUNT)

    assert response.status_code == 503
    assert (await _payment(integration_session)).settlement == xs.PENDING

    # Nothing was delivered, so recovery refunds it once.
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 1
    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == [AMOUNT]


async def test_unacknowledged_charge_commit_still_delivers(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    real_transition = xs._transition
    failed = False

    async def commit_then_fail(request_id: str, from_states: Any, to: str) -> bool:
        nonlocal failed
        changed = await real_transition(request_id, from_states, to)
        if to == xs.CHARGED and not failed:
            failed = True
            raise RuntimeError("connection lost after commit")
        return changed

    with patch.object(xs, "_transition", commit_then_fail):
        response = await _pay(api, _completion(), cost_sats=AMOUNT)

    # The charge is durable, so the paid response is delivered, not withheld.
    assert response.status_code == 200
    assert (await _payment(integration_session)).settlement == xs.CHARGED


async def test_shutdown_does_not_interrupt_a_recovery_mint(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)
    wallet.gate = asyncio.Event()
    recovery = asyncio.create_task(xs.recover_abandoned_payments())
    await asyncio.wait_for(wallet.started.wait(), 5)

    # Lifespan shutdown cancels the periodic task, then drains settlements.
    recovery.cancel()
    drain = asyncio.create_task(xs.drain_settlements(timeout=5))
    await asyncio.sleep(0.05)
    wallet.gate.set()
    await drain

    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert await _refunds(integration_session) == [AMOUNT]


async def test_unstored_recovery_refund_is_reclaimed_and_retried(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)
    reclaim = AsyncMock(return_value=(AMOUNT, "sat", MINT))
    with (
        patch.object(
            xs,
            "store_cashu_transaction_with_retry",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch.object(xs, "recieve_token", reclaim),
    ):
        assert await xs.recover_abandoned_payments() == 0

    reclaim.assert_awaited_once_with(f"cashuBrefund1x{AMOUNT}")
    assert (await _payment(integration_session)).settlement == xs.PENDING
    assert await _refunds(integration_session) == []

    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 1
    assert await _refunds(integration_session) == [AMOUNT]
    assert (await _claim(integration_session))["token"] == f"cashuBrefund2x{AMOUNT}"


async def test_unacknowledged_refund_commit_is_never_reclaimed(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)

    async def commit_then_fail(**kwargs: Any) -> bool:
        await store_cashu_transaction(**kwargs)
        raise RuntimeError("connection lost after commit")

    reclaim = AsyncMock()
    with (
        patch.object(xs, "store_cashu_transaction_with_retry", commit_then_fail),
        patch.object(xs, "recieve_token", reclaim),
    ):
        assert await xs.recover_abandoned_payments() == 1

    reclaim.assert_not_awaited()
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert (await _claim(integration_session))["token"] == f"cashuBrefund1x{AMOUNT}"


async def test_disabled_writer_does_not_reopen_reclaimed_change(
    wallet: FakeWallet,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet.fail = True
    await _pay("chat", _completion(), cost_sats=5)
    wallet.fail = False
    monkeypatch.setattr(settings, "x_cashu_retry_unminted_refunds", False)
    await _age(integration_session, STALE)
    with (
        patch.object(
            xs, "store_cashu_transaction_with_retry", AsyncMock(side_effect=OSError())
        ),
        patch.object(xs, "recieve_token", AsyncMock()) as reclaim,
    ):
        assert await xs.recover_abandoned_payments() == 0
    reclaim.assert_awaited_once()
    assert (await _payment(integration_session)).settlement == xs.FAILED
    assert await xs.recover_abandoned_payments() == 0


async def test_reclaimed_refund_reopens_despite_a_transient_failure(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)
    real_transition = xs._transition
    failed = False

    async def reopen_fails_once(
        request_id: str,
        from_states: Any,
        to: str,
        refund_amount: int | None = None,
    ) -> bool:
        nonlocal failed
        if to == xs.PENDING and not failed:
            failed = True
            raise RuntimeError("database unavailable")
        return await real_transition(request_id, from_states, to, refund_amount)

    with (
        patch.object(
            xs,
            "store_cashu_transaction_with_retry",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch.object(xs, "recieve_token", AsyncMock()),
        patch.object(xs, "_transition", reopen_fails_once),
    ):
        assert await xs.recover_abandoned_payments() == 0

    assert (await _payment(integration_session)).settlement == xs.PENDING


async def test_unstored_refund_that_cannot_be_reclaimed_is_terminal(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)
    with (
        patch.object(
            xs,
            "store_cashu_transaction_with_retry",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch.object(
            xs, "recieve_token", AsyncMock(side_effect=RuntimeError("mint down"))
        ),
    ):
        assert await xs.recover_abandoned_payments() == 0

    assert (await _payment(integration_session)).settlement == xs.FAILED
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == [AMOUNT]


async def test_failed_recovery_mint_is_retried(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    wallet.fail = True
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)

    # Mint selection failed before the wallet touched any proofs.
    assert await xs.recover_abandoned_payments() == 0
    assert (await _payment(integration_session)).settlement == xs.PENDING
    assert (await _claim(integration_session)).status_code == 425
    # Still owed until refunded, so payouts cannot spend the proofs.
    assert await total_user_liability(integration_session) == AMOUNT * 1000
    assert (
        await user_liability_for_mint_and_unit(integration_session, MINT, "sat")
        == AMOUNT * 1000
    )

    wallet.fail = False
    assert await xs.recover_abandoned_payments() == 1
    assert wallet.mints == [AMOUNT, AMOUNT]
    assert await _refunds(integration_session) == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert (await _claim(integration_session))["sats"] == str(AMOUNT)


async def test_recovery_refund_waits_out_mint_cooldown(
    wallet: FakeWallet,
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "primary_mint", MINT)
    monkeypatch.setattr(settings, "cashu_mints", [MINT])
    get_wallet = AsyncMock(side_effect=AssertionError("mint contacted"))
    monkeypatch.setattr(wallet_module, "get_wallet", get_wallet)
    guard = MintRateGuard.get(MINT)
    guard.apply_cooldown(300, reason="rate_limited")
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)

    # The real send_token finds no trusted mint out of cooldown.
    with patch.object(xs, "send_token", wallet_module.send_token):
        assert await xs.recover_abandoned_payments() == 0

    assert get_wallet.await_count == 0
    assert (await _payment(integration_session)).settlement == xs.PENDING
    assert (await _claim(integration_session)).status_code == 425

    guard._cooldown_until = 0.0
    assert await xs.recover_abandoned_payments() == 1
    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert (await _claim(integration_session))["sats"] == str(AMOUNT)


async def test_refund_that_cannot_be_reopened_is_closed_as_failed(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    transition = xs._transition

    async def reopen_fails(request_id: str, from_states: Any, to: str, *a: Any) -> bool:
        if to == xs.PENDING:
            raise RuntimeError("database unavailable")
        return await transition(request_id, from_states, to, *a)

    wallet.fail = True
    await _seed(integration_session, settlement=xs.PENDING, age=STALE)
    with patch.object(xs, "_transition", reopen_fails):
        assert await xs.recover_abandoned_payments() == 0
    assert (await _payment(integration_session)).settlement == xs.REFUNDING

    # A stale refunding row is closed without minting again.
    wallet.fail = False
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 0
    assert (await _payment(integration_session)).settlement == xs.FAILED
    assert wallet.mints == [AMOUNT]


async def test_live_refund_repairs_stale_closure_race(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.REFUNDING, age=STALE)
    checked = asyncio.Event()
    release = asyncio.Event()
    real_has_stored_refund = xs._has_stored_refund

    async def stale_check(request_id: str) -> bool:
        stored = await real_has_stored_refund(request_id)
        checked.set()
        await release.wait()
        return stored

    with patch.object(xs, "_has_stored_refund", stale_check):
        closing = asyncio.create_task(
            xs._close_stale_refund(await _payment(integration_session))
        )
        await asyncio.wait_for(checked.wait(), 5)
        await store_cashu_transaction(
            token="cashuBlive", amount=15, unit="sat", typ="out", request_id="req-876"
        )
        release.set()
        await closing

    assert (await _payment(integration_session)).settlement == xs.FAILED
    await xs.finish_refund("req-876", refunded=True)

    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert await total_user_liability(integration_session) == 0
    assert (await _claim(integration_session))["token"] == "cashuBlive"


@pytest.mark.parametrize("stored", [False, True])
async def test_interrupted_refund_is_never_minted_again(
    wallet: FakeWallet, integration_session: AsyncSession, stored: bool
) -> None:
    await _seed(integration_session, settlement=xs.REFUNDING, age=STALE)
    if stored:
        await store_cashu_transaction(
            token="cashuBearlier", amount=7, unit="sat", typ="out", request_id="req-876"
        )

    assert await xs.recover_abandoned_payments() == 0

    assert wallet.mints == []
    payment = await _payment(integration_session)
    assert payment.settlement == (xs.REFUNDED if stored else xs.FAILED)
    claimed = await _claim(integration_session)
    if stored:
        assert claimed["token"] == "cashuBearlier"
    else:
        assert claimed.status_code == 409


async def test_refund_in_progress_answers_pending_until_stale(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.REFUNDING, age=10)
    assert (await _claim(integration_session)).status_code == 425

    await _age(integration_session, STALE)
    assert (await _claim(integration_session)).status_code == 409


async def test_payments_from_before_tracking_are_never_auto_refunded(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=None, age=STALE * 10)

    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == []
    error = await _claim(integration_session)
    assert error.status_code == 409
    assert error.detail["error"]["code"] == "refund_not_recorded"


async def test_recent_payment_from_before_tracking_answers_pending(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=None, age=10)
    assert (await _claim(integration_session)).status_code == 425


# --- collection versus the refund sweeper -----------------------------------


async def test_refund_claimed_by_the_sweeper_is_not_handed_out(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.REFUNDED, age=10)
    await store_cashu_transaction(
        token="cashuBchange", amount=7, unit="sat", typ="out", request_id="req-876"
    )
    await integration_session.exec(  # type: ignore[call-overload]
        update(CashuTransaction)
        .where(col(CashuTransaction.token) == "cashuBchange")
        .values(sweep_started_at=int(time.time()))
    )
    await integration_session.commit()

    assert (await _claim(integration_session)).status_code == 425
    change = await _payment_out(integration_session)
    assert change.collected is False


async def test_collected_refund_is_not_swept(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _seed(integration_session, settlement=xs.REFUNDED, age=10)
    await store_cashu_transaction(
        token="cashuBchange",
        amount=7,
        unit="sat",
        typ="out",
        request_id="req-876",
        created_at=int(time.time()) - 1000,
    )
    assert (await _claim(integration_session))["token"] == "cashuBchange"

    redeem = AsyncMock(return_value=(7, "sat", MINT))
    with patch("routstr.wallet.recieve_token", redeem):
        await _refund_sweep_once(int(time.time()))

    redeem.assert_not_awaited()
    assert (await _payment_out(integration_session)).swept is False


async def _payment_out(session: AsyncSession) -> CashuTransaction:
    session.expire_all()
    result = await session.exec(
        select(CashuTransaction).where(CashuTransaction.token == "cashuBchange")
    )
    return result.one()


# --- shutdown -----------------------------------------------------------------


async def test_shutdown_drain_waits_for_cancelled_paid_requests(
    wallet: FakeWallet, integration_session: AsyncSession, api: str
) -> None:
    async def hang(*_: Any, **__: Any) -> Any:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_pay(api, hang))
    while (await _payment_or_none(integration_session)) is None:
        await asyncio.sleep(0.01)

    # Uvicorn cancels the request and runs lifespan shutdown straight away,
    # before the request's refund handler has had a chance to run.
    task.cancel()
    await xs.drain_settlements(timeout=5)

    assert task.done()
    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED


async def _payment_or_none(session: AsyncSession) -> CashuTransaction | None:
    session.expire_all()
    result = await session.exec(
        select(CashuTransaction).where(CashuTransaction.type == "in")
    )
    return result.first()


async def test_shutdown_drain_waits_for_started_refunds() -> None:
    finished = asyncio.Event()

    async def refund() -> None:
        await asyncio.sleep(0.05)
        finished.set()

    xs.run_to_completion(refund())
    await xs.drain_settlements(timeout=5)
    assert finished.is_set()


# --- EHBP -----------------------------------------------------------------


def _ehbp_upstream() -> tuple[MagicMock, MagicMock]:
    target = MagicMock()
    target.url = "https://inference.tinfoil.sh/v1/chat/completions"
    target.headers = {}
    target.profile = None
    profile = MagicMock()
    profile.client_target_url_header = None
    profile.allow_client_target_override = False
    profile.proxy_only_headers = frozenset()
    profile.usage_response_header = None
    upstream = MagicMock()
    upstream.prepare_headers.return_value = {}
    upstream.get_ehbp_forwarding_target.return_value = target
    upstream.get_confidential_inference_profile.return_value = profile
    upstream.prepare_params.return_value = {}
    upstream.provider_type = "tinfoil"
    model_obj = MagicMock()
    model_obj.id = "tinfoil-model"
    return upstream, model_obj


async def _ehbp_pay(forward: AsyncMock, cost_msats: int) -> Any:
    request = MagicMock()
    request.state.request_id = "req-876"
    request.method = "POST"
    request.query_params = {}
    request.headers = {}
    request.body = AsyncMock(return_value=b"opaque")
    upstream, model_obj = _ehbp_upstream()
    with (
        patch.object(ehbp_module, "forward_with_trailer", forward),
        patch.object(
            ehbp_module,
            "_compute_ehbp_actual_cost",
            AsyncMock(
                return_value={
                    "total_msats": cost_msats,
                    "computed_msats": cost_msats,
                    "input_msats": 0,
                    "output_msats": cost_msats,
                }
            ),
        ),
    ):
        return await ehbp_module.forward_ehbp_x_cashu_request(
            request=request,
            x_cashu_token=PAYMENT,
            path="v1/chat/completions",
            max_cost_for_model=AMOUNT * 1000,
            model_obj=model_obj,
            upstream=upstream,
        )


def _ehbp_ok() -> TrailerResponse:
    return TrailerResponse(
        status_code=200,
        headers=[("content-type", "application/octet-stream")],
        body=b"ciphertext",
    )


async def test_ehbp_full_charge_is_terminal(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    await _ehbp_pay(AsyncMock(return_value=_ehbp_ok()), cost_msats=AMOUNT * 1000)

    assert wallet.mints == []
    assert (await _payment(integration_session)).settlement == xs.CHARGED


async def test_ehbp_change_is_refunded(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    response = await _ehbp_pay(AsyncMock(return_value=_ehbp_ok()), cost_msats=5000)

    assert response.headers["X-Cashu"] == "cashuBrefund1x15"
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert (await _claim(integration_session))["sats"] == "15"


async def test_ehbp_full_charge_is_withheld_when_it_cannot_be_recorded(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    with patch.object(xs, "mark_charged", AsyncMock(return_value=False)):
        response = await _ehbp_pay(
            AsyncMock(return_value=_ehbp_ok()), cost_msats=AMOUNT * 1000
        )

    assert response.status_code == 503
    assert b"ciphertext" not in bytes(response.body)


async def test_ehbp_failed_full_refund_mint_is_retried_by_recovery(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    wallet.fail = True
    response = await _ehbp_pay(
        AsyncMock(side_effect=RuntimeError("upstream down")), 5000
    )

    assert response.status_code == 500
    assert b"/v1/balance/refund" in bytes(response.body)
    assert (await _payment(integration_session)).settlement == xs.PENDING

    wallet.fail = False
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 1
    assert wallet.mints == [AMOUNT, AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED


async def test_ehbp_failed_change_mint_is_retried_by_recovery_for_the_change(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    wallet.fail = True
    response = await _ehbp_pay(AsyncMock(return_value=_ehbp_ok()), cost_msats=5000)

    # Inference was paid for, so it is served while recovery owes the change.
    assert response.status_code == 200
    assert response.headers[X_ROUTSTR_REFUND_ERROR_HEADER] == "refund_failed"
    assert "X-Cashu" not in response.headers
    assert b"".join([chunk async for chunk in response.body_iterator]) == (
        b"ciphertext"
    )
    payment = await _payment(integration_session)
    assert payment.settlement == xs.PENDING
    assert payment.refund_amount == 15

    wallet.fail = False
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 1
    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == [15, 15]
    assert await _refunds(integration_session) == [15]
    assert (await _claim(integration_session))["sats"] == "15"


async def test_ehbp_unstored_refund_token_is_terminal(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    async def out_row_fails(**kwargs: Any) -> bool:
        if kwargs["typ"] == "out":
            raise RuntimeError("database unavailable")
        return await store_cashu_transaction(**kwargs)

    with patch.object(ehbp_module, "store_cashu_transaction", out_row_fails):
        response = await _ehbp_pay(
            AsyncMock(side_effect=RuntimeError("upstream down")), 5000
        )

    # The token was minted and handed out, so it must never be minted again.
    assert response.headers["X-Cashu"] == f"cashuBrefund1x{AMOUNT}"
    assert (await _payment(integration_session)).settlement == xs.FAILED
    await _age(integration_session, STALE)
    assert await xs.recover_abandoned_payments() == 0
    assert wallet.mints == [AMOUNT]


async def test_ehbp_cancellation_refunds_in_full(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    with pytest.raises(asyncio.CancelledError):
        await _ehbp_pay(AsyncMock(side_effect=asyncio.CancelledError()), 5000)

    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED


async def test_ehbp_cancellation_during_pending_write_keeps_refund_claimable(
    wallet: FakeWallet, integration_session: AsyncSession
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_store(**kwargs: Any) -> bool:
        started.set()
        await release.wait()
        return await store_cashu_transaction(**kwargs)

    with patch.object(ehbp_module, "store_cashu_transaction", delayed_store):
        task = asyncio.create_task(_ehbp_pay(AsyncMock(return_value=_ehbp_ok()), 5000))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert wallet.mints == [AMOUNT]
    assert (await _payment(integration_session)).settlement == xs.REFUNDED
    assert (await _claim(integration_session))["sats"] == str(AMOUNT)
