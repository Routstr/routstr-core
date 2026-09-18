"""Regression tests for the production "negative available balance" 402.

Invariants protected:
* billing and the admin API agree on what "available" means
* an in-flight (heartbeaten) reservation is never swept; an abandoned one is
  released and can never be charged afterwards
* a cost overrun spends only its own reservation plus unreserved balance
* corrupt reservations are repaired terminally instead of poisoning cleanup
* under concurrency, balance / reserved / available never go negative
"""

import asyncio
import random
import time
import uuid
from typing import Awaitable, Callable
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel import col, func, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey, ReservationRelease
from routstr.payment.cost_calculation import CostData

pytestmark = pytest.mark.integration

# Realistic sweeper timeout: a renewed (heartbeaten) reservation stays alive,
# a reservation backdated past this is released.
STALE_TIMEOUT_SECONDS = 300

# created_at is whole seconds; -1 makes every reservation stale immediately.
# Only used in the fuzz test to stress the terminal-release path.
SWEEP_EVERYTHING = -1


def _cost_data(total_msats: int) -> CostData:
    return CostData(
        base_msats=0,
        input_msats=total_msats // 2,
        output_msats=total_msats - total_msats // 2,
        total_msats=total_msats,
        total_usd=0.0,
        input_tokens=100,
        output_tokens=100,
    )


def _response(model: str = "test-model") -> dict:
    return {
        "model": model,
        "usage": {"prompt_tokens": 100, "completion_tokens": 100},
    }


async def _new_key(session: AsyncSession, balance: int) -> str:
    key_hash = f"test_neg_{uuid.uuid4().hex}"
    session.add(
        ApiKey(
            hashed_key=key_hash,
            balance=balance,
            reserved_balance=0,
            total_spent=0,
            total_requests=0,
        )
    )
    await session.commit()
    return key_hash


async def _backdate_reservation(
    session: AsyncSession, release_id: str, seconds: int
) -> None:
    """Age a reservation's lease as if it had not been renewed for `seconds`."""
    await session.exec(  # type: ignore[call-overload]
        update(ReservationRelease)
        .where(col(ReservationRelease.id) == release_id)
        .values(created_at=col(ReservationRelease.created_at) - seconds)
    )
    await session.commit()


async def _wait_for(
    predicate: "Callable[[], Awaitable[bool]]",
    timeout: float = 10.0,
    interval: float = 0.1,
) -> bool:
    """Bounded polling instead of fixed sleeps for background-task effects."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if await predicate():
            return True
        if asyncio.get_event_loop().time() > deadline:
            return False
        await asyncio.sleep(interval)


@pytest.mark.asyncio
async def test_402_reports_negative_available_while_admin_shows_positive(
    integration_session: AsyncSession,
    integration_client: AsyncClient,
) -> None:
    """The production symptom: billing rejects on balance - reserved_balance,
    so the admin endpoint must expose reserved/available, not just balance."""
    from fastapi import HTTPException

    from routstr.auth import _validate_bearer_key_locked
    from routstr.core.db import set_admin_password

    key_hash = await _new_key(integration_session, balance=263_000)
    # Leaked reservations slightly exceeding the balance, as in production.
    await integration_session.exec(  # type: ignore[call-overload]
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key_hash)
        .values(reserved_balance=267_215)
    )
    await integration_session.commit()

    with pytest.raises(HTTPException) as exc:
        await _validate_bearer_key_locked(
            "sk-" + key_hash, integration_session, min_cost=1
        )

    assert exc.value.status_code == 402
    message = exc.value.detail["error"]["message"]  # type: ignore[index]
    assert "-4.215 sats (-4215 msats) available" in message, message

    await set_admin_password(integration_session, "test-admin-pw")
    login = await integration_client.post(
        "/admin/api/login", json={"password": "test-admin-pw"}
    )
    assert login.status_code == 200
    token = login.json()["token"]
    resp = await integration_client.get(
        "/admin/api/temporary-balances",
        params={"search": key_hash},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    rows = [row for row in resp.json()["balances"] if row["hashed_key"] == key_hash]
    assert len(rows) == 1
    row = rows[0]
    assert row["balance"] == 263_000
    assert row["reserved_balance"] == 267_215
    assert row["available_balance"] == -4_215
    assert resp.json()["totals"] == {
        "total_balance": 263_000,
        "total_reserved_balance": 267_215,
        "total_available_balance": -4_215,
        "total_spent": 0,
        "total_requests": 0,
    }


@pytest.mark.asyncio
async def test_abandoned_reservation_is_swept_and_cannot_finalize(
    integration_session: AsyncSession,
) -> None:
    """Release is terminal: a swept reservation's late finalizer must not
    charge, or it could spend funds since reserved by another request."""
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )
    from routstr.core.db import release_stale_reservations

    cost = 5_000
    key_hash = await _new_key(integration_session, balance=10_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, cost, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    # Client vanished: the lease is never renewed and ages past the timeout.
    await _backdate_reservation(
        integration_session, reservation.release_id, STALE_TIMEOUT_SECONDS + 1
    )
    released = await release_stale_reservations(
        integration_session, STALE_TIMEOUT_SECONDS
    )
    assert released == 1

    # A zombie finalizer shows up afterwards; it must not charge.
    with patch("routstr.auth.calculate_cost", return_value=_cost_data(cost)):
        await adjust_payment_for_tokens(
            key,
            _response(),
            integration_session,
            cost,
            reservation_snapshot=reservation,
        )

    await integration_session.refresh(key)
    record = await integration_session.get(ReservationRelease, reservation.release_id)
    assert record is not None and record.status == "released"
    assert key.total_spent == 0
    assert key.balance == 10_000
    assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_sweeper_cannot_release_a_reservation_renewed_after_selection(
    integration_session: AsyncSession,
) -> None:
    """A heartbeat landing between the sweeper's select and its transition
    must win — exercised through the public sweeper entry point."""
    from routstr.auth import (
        get_reservation_snapshot,
        pay_for_request,
        renew_reservation,
    )
    from routstr.core import db as core_db
    from routstr.core.db import release_stale_reservations

    key_hash = await _new_key(integration_session, balance=1_000)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None
    await pay_for_request(key, 1_000, integration_session)
    reservation = await get_reservation_snapshot(key, integration_session)

    await _backdate_reservation(
        integration_session, reservation.release_id, STALE_TIMEOUT_SECONDS + 100
    )

    real_transition = core_db._transition_stale_reservation

    async def renew_then_transition(
        session: AsyncSession, reservation_id: str, cutoff: int
    ) -> bool:
        # The sweeper selected this reservation as stale; the heartbeat
        # renews exactly between that select and the transition.
        assert await renew_reservation(reservation, session)
        return await real_transition(session, reservation_id, cutoff)

    with patch.object(core_db, "_transition_stale_reservation", renew_then_transition):
        released = await release_stale_reservations(
            integration_session, STALE_TIMEOUT_SECONDS
        )

    assert released == 0
    record = await integration_session.get(ReservationRelease, reservation.release_id)
    assert record is not None and record.status == "active"
    await integration_session.refresh(key)
    assert key.reserved_balance == 1_000


@pytest.mark.asyncio
async def test_legacy_cleanup_cannot_erase_a_reservation_committed_after_its_read(
    integration_session: AsyncSession,
) -> None:
    """A reservation committing between the legacy sweep's read and its
    zeroing must survive — exercised through the public sweeper entry point."""
    from routstr.core import db as core_db
    from routstr.core.db import release_stale_reservations

    key_hash = await _new_key(integration_session, balance=10_000)
    stale_reserved_at = int(time.time()) - (STALE_TIMEOUT_SECONDS + 100)
    await integration_session.exec(  # type: ignore[call-overload]
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == key_hash)
        .values(reserved_balance=500, reserved_at=stale_reserved_at)
    )
    await integration_session.commit()

    real_release = core_db._release_legacy_aggregate

    async def commit_reservation_then_release(
        session: AsyncSession,
        target_key_hash: str,
        observed_reserved: int,
        observed_reserved_at: int | None,
    ) -> bool:
        # The sweeper read the legacy aggregate and found no active durable
        # owner; a new reservation commits exactly before the zeroing lands.
        session.add(
            ReservationRelease(
                id=uuid.uuid4().hex,
                key_hash=target_key_hash,
                billing_key_hash=target_key_hash,
                reserved_msats=700,
                status="active",
            )
        )
        await session.exec(  # type: ignore[call-overload]
            update(ApiKey)
            .where(col(ApiKey.hashed_key) == target_key_hash)
            .values(
                reserved_balance=col(ApiKey.reserved_balance) + 700,
                reserved_at=int(time.time()),
            )
        )
        await session.commit()
        return await real_release(
            session, target_key_hash, observed_reserved, observed_reserved_at
        )

    with patch.object(
        core_db, "_release_legacy_aggregate", commit_reservation_then_release
    ):
        released = await release_stale_reservations(
            integration_session, STALE_TIMEOUT_SECONDS
        )

    assert released == 0
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None
    assert key.reserved_balance == 1_200, "legacy cleanup erased a live reservation"


@pytest.mark.asyncio
async def test_sweeper_repairs_corrupt_reservation_and_continues_batch(
    integration_session: AsyncSession,
) -> None:
    """One corrupt durable reservation (aggregate no longer holds its msats)
    must be terminalized without aggregate subtraction, and must not stop the
    rest of the batch from being released normally."""
    from routstr.auth import get_reservation_snapshot, pay_for_request
    from routstr.core.db import release_stale_reservations

    cost = 1_000
    corrupt_hash = await _new_key(integration_session, balance=cost)
    healthy_hash = await _new_key(integration_session, balance=cost)

    corrupt_key = await integration_session.get(ApiKey, corrupt_hash)
    assert corrupt_key is not None
    await pay_for_request(corrupt_key, cost, integration_session)
    corrupt_reservation = await get_reservation_snapshot(
        corrupt_key, integration_session
    )

    healthy_key = await integration_session.get(ApiKey, healthy_hash)
    assert healthy_key is not None
    await pay_for_request(healthy_key, cost, integration_session)
    healthy_reservation = await get_reservation_snapshot(
        healthy_key, integration_session
    )

    # Corrupt the first key: its aggregate no longer holds the reservation.
    await integration_session.exec(  # type: ignore[call-overload]
        update(ApiKey)
        .where(col(ApiKey.hashed_key) == corrupt_hash)
        .values(reserved_balance=0)
    )
    await integration_session.commit()

    for reservation in (corrupt_reservation, healthy_reservation):
        await _backdate_reservation(
            integration_session, reservation.release_id, STALE_TIMEOUT_SECONDS + 100
        )

    released = await release_stale_reservations(
        integration_session, STALE_TIMEOUT_SECONDS
    )
    assert released == 2, "a corrupt record must not abort the sweep batch"

    for reservation in (corrupt_reservation, healthy_reservation):
        record = await integration_session.get(
            ReservationRelease, reservation.release_id
        )
        assert record is not None and record.status == "released"
    healthy_key = await integration_session.get(ApiKey, healthy_hash)
    corrupt_key = await integration_session.get(ApiKey, corrupt_hash)
    assert healthy_key is not None and corrupt_key is not None
    assert healthy_key.reserved_balance == 0
    assert corrupt_key.reserved_balance == 0
    assert corrupt_key.balance == cost, "repair must not touch balances"


@pytest.mark.asyncio
async def test_heartbeat_survives_a_rolled_back_charge_attempt(
    integration_session: AsyncSession,
    patched_db_engine: None,
) -> None:
    """Claiming a reservation must not stop its heartbeat: a rollback restores
    the active reservation, which then still needs lease renewal."""
    from routstr import auth
    from routstr.auth import (
        _claim_reservation_for_charge,
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )
    from routstr.core.db import create_session

    cost = 1_000
    timeout = 3  # heartbeat interval = 1s
    with patch.object(auth.settings, "stale_reservation_timeout_seconds", timeout):
        async with create_session() as session:
            key_hash = await _new_key(session, balance=2_000)
            key = await session.get(ApiKey, key_hash)
            assert key is not None
            await pay_for_request(key, cost, session)
            reservation = await get_reservation_snapshot(key, session)

        try:
            # A charge attempt claims the reservation, then its transaction
            # fails and rolls back.
            async with create_session() as session:
                assert await _claim_reservation_for_charge(reservation, session)
                await session.rollback()

            async with create_session() as session:
                record = await session.get(ReservationRelease, reservation.release_id)
                assert record is not None and record.status == "active"
                await _backdate_reservation(
                    session, reservation.release_id, timeout * 10
                )
                record = await session.get(ReservationRelease, reservation.release_id)
                assert record is not None
                backdated_lease = record.created_at

            async def lease_renewed() -> bool:
                async with create_session() as session:
                    record = await session.get(
                        ReservationRelease, reservation.release_id
                    )
                    return record is not None and record.created_at > backdated_lease

            assert await _wait_for(lease_renewed), (
                "heartbeat did not survive the rolled-back charge attempt"
            )

            # The restored reservation still finalizes normally.
            with patch("routstr.auth.calculate_cost", return_value=_cost_data(cost)):
                async with create_session() as session:
                    key = await session.get(ApiKey, key_hash)
                    assert key is not None
                    result = await adjust_payment_for_tokens(
                        key,
                        _response(),
                        session,
                        cost,
                        reservation_snapshot=reservation,
                    )
        finally:
            await auth._stop_reservation_heartbeat(reservation.release_id)

    assert result["charged_msats"] == cost
    async with create_session() as session:
        record = await session.get(ReservationRelease, reservation.release_id)
        assert record is not None and record.status == "charged"
        key = await session.get(ApiKey, key_hash)
        assert key is not None
        assert key.total_spent == cost


@pytest.mark.asyncio
async def test_heartbeat_dies_with_its_request_so_sweeper_can_recover(
    integration_session: AsyncSession,
    patched_db_engine: None,
) -> None:
    """A request that vanishes without finalizing must not renew forever —
    its heartbeat stops with the owning task and the sweeper reclaims the
    funds."""
    from routstr import auth
    from routstr.auth import get_reservation_snapshot, pay_for_request
    from routstr.core.db import create_session, release_stale_reservations

    cost = 1_000
    timeout = 3  # heartbeat interval = 1s
    async with create_session() as session:
        key_hash = await _new_key(session, balance=cost)

    holder: dict = {}
    with patch.object(auth.settings, "stale_reservation_timeout_seconds", timeout):

        async def doomed_request() -> None:
            async with create_session() as session:
                key = await session.get(ApiKey, key_hash)
                assert key is not None
                await pay_for_request(key, cost, session)
                holder["reservation"] = await get_reservation_snapshot(key, session)
            # ...request control dies here, no finalize and no release.

        await asyncio.create_task(doomed_request())
        release_id = holder["reservation"].release_id

        try:
            # While the heartbeat is still winding down it may renew once
            # more; keep backdating until the sweeper wins, which it must as
            # soon as the dead owner is noticed.
            async def sweeper_recovered() -> bool:
                async with create_session() as session:
                    await _backdate_reservation(session, release_id, timeout * 10)
                    return await release_stale_reservations(session, timeout) == 1

            assert await _wait_for(sweeper_recovered), (
                "sweeper never recovered the abandoned reservation"
            )
        finally:
            await auth._stop_reservation_heartbeat(release_id)

        async with create_session() as session:
            record = await session.get(ReservationRelease, release_id)
            assert record is not None and record.status == "released"
            key = await session.get(ApiKey, key_hash)
            assert key is not None
            assert key.reserved_balance == 0
            assert key.total_spent == 0


@pytest.mark.asyncio
async def test_reservation_heartbeat_covers_the_whole_request_lifecycle(
    integration_session: AsyncSession,
    patched_db_engine: None,
) -> None:
    """pay_for_request starts the heartbeat, finalization stops it, and a
    backdated lease is renewed in the background without any manual call."""
    from routstr import auth
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )
    from routstr.core.db import create_session, release_stale_reservations

    cost = 1_000
    timeout = 3  # heartbeat interval = 1s
    async with create_session() as session:
        key_hash = await _new_key(session, balance=2 * cost)

    with patch.object(auth.settings, "stale_reservation_timeout_seconds", timeout):
        async with create_session() as session:
            key = await session.get(ApiKey, key_hash)
            assert key is not None
            await pay_for_request(key, cost, session)
            reservation = await get_reservation_snapshot(key, session)

        try:
            # Only a background renewal can keep this alive now.
            async with create_session() as session:
                await _backdate_reservation(
                    session, reservation.release_id, timeout * 10
                )
                record = await session.get(ReservationRelease, reservation.release_id)
                assert record is not None
                backdated_lease = record.created_at

            async def lease_renewed() -> bool:
                async with create_session() as session:
                    record = await session.get(
                        ReservationRelease, reservation.release_id
                    )
                    return record is not None and record.created_at > backdated_lease

            assert await _wait_for(lease_renewed), "heartbeat never renewed the lease"
            async with create_session() as session:
                assert await release_stale_reservations(session, timeout) == 0

            with patch("routstr.auth.calculate_cost", return_value=_cost_data(cost)):
                async with create_session() as session:
                    key = await session.get(ApiKey, key_hash)
                    assert key is not None
                    result = await adjust_payment_for_tokens(
                        key,
                        _response(),
                        session,
                        cost,
                        reservation_snapshot=reservation,
                    )
        finally:
            await auth._stop_reservation_heartbeat(reservation.release_id)

    assert result["charged_msats"] == cost
    async with create_session() as session:
        record = await session.get(ReservationRelease, reservation.release_id)
        assert record is not None and record.status == "charged"
        key = await session.get(ApiKey, key_hash)
        assert key is not None
        assert key.total_spent == cost
        assert key.reserved_balance == 0


@pytest.mark.asyncio
async def test_overrun_cannot_spend_a_concurrent_reservation(
    integration_session: AsyncSession,
) -> None:
    """An overrun is capped to its own reservation plus unreserved balance;
    the sibling's reserved funds stay untouched and available stays >= 0."""
    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )

    reserved_each = 100
    overrun_cost = 150  # A's real token cost exceeds its reservation

    # Balance covers exactly two reservations; nothing free on top.
    key_hash = await _new_key(integration_session, balance=2 * reserved_each)
    key = await integration_session.get(ApiKey, key_hash)
    assert key is not None

    await pay_for_request(key, reserved_each, integration_session)
    reservation_a = await get_reservation_snapshot(key, integration_session)
    await pay_for_request(key, reserved_each, integration_session)
    await get_reservation_snapshot(key, integration_session)  # B stays in flight

    await integration_session.refresh(key)
    assert key.reserved_balance == 2 * reserved_each

    # Only A finalizes; B is still streaming and its funds must stay reserved.
    with patch("routstr.auth.calculate_cost", return_value=_cost_data(overrun_cost)):
        result = await adjust_payment_for_tokens(
            key,
            _response(),
            integration_session,
            reserved_each,
            reservation_snapshot=reservation_a,
        )

    assert result["charged_msats"] == reserved_each
    assert result["total_msats"] == overrun_cost
    await integration_session.refresh(key)
    assert key.total_spent == reserved_each
    assert key.reserved_balance == reserved_each
    assert key.balance == reserved_each
    assert key.total_balance >= 0, (
        f"available balance went negative: balance={key.balance} "
        f"reserved={key.reserved_balance} -> {key.total_balance} msats; "
        "the overrun charge consumed the still-reserved funds of request B"
    )


@pytest.mark.asyncio
async def test_concurrent_requests_with_sweeper_keep_balance_invariants(
    integration_session: AsyncSession,
    patched_db_engine: None,
) -> None:
    """Fuzz: concurrent requests against an everything-is-stale sweeper.
    Some requests legitimately finish uncharged (release is terminal), but
    balances must never go negative and no reservation may stay active."""
    from fastapi import HTTPException

    from routstr.auth import (
        adjust_payment_for_tokens,
        get_reservation_snapshot,
        pay_for_request,
    )
    from routstr.core.db import create_session, release_stale_reservations

    rng = random.Random(1337)
    starting_balance = 200_000
    n_requests = 24

    async with create_session() as session:
        key_hash = await _new_key(session, balance=starting_balance)

    completed_costs: list[int] = []
    rejected_requests = 0

    async def one_request(index: int) -> None:
        nonlocal rejected_requests
        reserved = rng.randrange(1_000, 4_000)
        actual = max(1, int(reserved * rng.uniform(0.5, 1.1)))
        try:
            async with create_session() as session:
                key = await session.get(ApiKey, key_hash)
                assert key is not None
                await pay_for_request(key, reserved, session)
        except HTTPException as exc:
            # A depleted balance is the only legitimate rejection.
            assert exc.status_code == 402, exc.detail
            rejected_requests += 1
            return

        try:
            async with create_session() as session:
                key = await session.get(ApiKey, key_hash)
                assert key is not None
                reservation = await get_reservation_snapshot(key, session)
        except RuntimeError:
            # The everything-is-stale sweeper can release the reservation
            # before the stream even starts; the request aborts uncharged.
            return

        await asyncio.sleep(rng.uniform(0, 0.02))  # the "stream"

        async with create_session() as session:
            key = await session.get(ApiKey, key_hash)
            assert key is not None
            await adjust_payment_for_tokens(
                key,
                _response(str(actual)),
                session,
                reserved,
                reservation_snapshot=reservation,
            )
        completed_costs.append(actual)

    sweeping = True

    async def sweeper() -> None:
        while sweeping:
            async with create_session() as session:
                await release_stale_reservations(session, SWEEP_EVERYTHING)
            await asyncio.sleep(0.002)

    sweep_task = asyncio.create_task(sweeper())
    try:
        with patch(
            "routstr.auth.calculate_cost",
            side_effect=lambda response_data, *a, **k: _cost_data(
                int(response_data["model"])
            ),
        ):
            await asyncio.gather(*(one_request(i) for i in range(n_requests)))
    finally:
        sweeping = False
        await sweep_task

    async with create_session() as session:
        key = await session.get(ApiKey, key_hash)
        assert key is not None
        leftover_active = (
            await session.exec(  # type: ignore[call-overload]
                select(func.count())
                .select_from(ReservationRelease)
                .where(col(ReservationRelease.status) == "active")
            )
        ).one()

    assert completed_costs or rejected_requests, "no request made any progress"
    assert key.balance >= 0, f"balance went negative: {key.balance}"
    assert key.reserved_balance >= 0, (
        f"reserved_balance went negative: {key.reserved_balance}"
    )
    assert key.total_balance >= 0, (
        f"available balance negative: balance={key.balance} "
        f"reserved={key.reserved_balance} (this is the production symptom)"
    )
    assert key.total_spent <= starting_balance, (
        f"spent {key.total_spent} of a {starting_balance} balance"
    )
    # Requests whose reservation was swept mid-flight finish uncharged, so the
    # charged total can only be at most the sum of completed request costs.
    max_expected_spend = sum(completed_costs)
    assert key.total_spent <= max_expected_spend, (
        f"charged {key.total_spent} msats but completed requests only cost "
        f"{max_expected_spend}"
    )
    assert leftover_active == 0, (
        f"{leftover_active} reservations still active after all requests finished"
    )
