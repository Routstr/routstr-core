from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

import routstr.core.terminal_outcomes as outcomes_module
from routstr.core.db import (
    TerminalOutcome,
    TerminalOutcomeEpoch,
    TerminalOutcomeWriterRun,
)
from routstr.core.terminal_outcomes import (
    TerminalOutcomeContext,
    TerminalOutcomeWriter,
    _PersistResult,
    cashu_retained_msats,
    record_terminal_outcome,
)


@dataclass
class MutableClock:
    value: int

    def __call__(self) -> int:
        return self.value


@pytest.fixture
async def ledger(
    tmp_path: Path,
) -> AsyncGenerator[tuple[AsyncEngine, outcomes_module.SessionFactory], None]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'terminal-outcomes.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def sessions() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    yield engine, sessions
    await engine.dispose()


def _timestamp(day: date, hour: int = 12) -> int:
    return int(
        datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp() * 1000
    )


def _record(
    outcome_id: str,
    timestamp_ms: int,
    *,
    revenue_msats: int = 2000,
) -> None:
    record_terminal_outcome(
        TerminalOutcomeContext(outcome_id, "author/model"),
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=2,
        cache_creation_input_tokens=1,
        revenue_msats=revenue_msats,
        terminal_at_ms=timestamp_ms,
    )


async def test_writer_persists_one_immutable_outcome_and_closes_own_run(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = ledger
    day = date(2026, 8, 31)
    timestamp = _timestamp(day)
    writer = TerminalOutcomeWriter(
        session_factory=sessions,
        retry_seconds=0.01,
        heartbeat_seconds=0.05,
        lease_timeout_seconds=1,
        clock=MutableClock(timestamp),
    )
    monkeypatch.setattr(outcomes_module, "terminal_outcome_writer", writer)

    assert await writer.start()
    _record("request-1", timestamp)
    _record("request-1", timestamp)
    assert await writer.flush(timeout=1)

    async with sessions() as session:
        stored = (await session.exec(select(TerminalOutcome))).all()
        epochs = (await session.exec(select(TerminalOutcomeEpoch))).all()
        runs = (await session.exec(select(TerminalOutcomeWriterRun))).all()
    assert len(stored) == 1
    assert stored[0].terminal_day == day
    assert stored[0].input_observed is None
    assert stored[0].revenue_msats == 2000
    assert len(epochs) == 1
    assert epochs[0].coverage_start_day == day + timedelta(days=1)
    assert len(runs) == 1 and runs[0].status == "active"

    assert await writer.stop(timeout=1)
    async with sessions() as session:
        run = (await session.exec(select(TerminalOutcomeWriterRun))).one()
    assert run.status == "clean"
    assert run.closed_at_ms == timestamp


async def test_writer_allocates_after_latest_closed_epoch(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
) -> None:
    _, sessions = ledger
    day = date(2026, 8, 31)
    async with sessions() as session:
        session.add(
            TerminalOutcomeEpoch(
                epoch=0,
                coverage_start_day=day - timedelta(days=2),
                coverage_end_day=day - timedelta(days=1),
                current_slot=None,
            )
        )
        await session.commit()
    writer = TerminalOutcomeWriter(
        session_factory=sessions,
        clock=MutableClock(_timestamp(day)),
    )

    assert await writer.start()
    assert await writer.stop(timeout=1)
    async with sessions() as session:
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
    assert [
        (epoch.epoch, epoch.coverage_start_day, epoch.current_slot) for epoch in epochs
    ] == [
        (0, day - timedelta(days=2), None),
        (1, day + timedelta(days=1), 1),
    ]


async def test_conflicting_duplicate_rotates_epoch_without_overwriting(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = ledger
    day = date(2026, 8, 31)
    timestamp = _timestamp(day)
    writer = TerminalOutcomeWriter(
        session_factory=sessions,
        retry_seconds=0.01,
        heartbeat_seconds=0.05,
        lease_timeout_seconds=1,
        clock=MutableClock(timestamp),
    )
    monkeypatch.setattr(outcomes_module, "terminal_outcome_writer", writer)

    assert await writer.start()
    _record("request-conflict", timestamp, revenue_msats=1000)
    assert await writer.flush(timeout=1)
    _record("request-conflict", timestamp, revenue_msats=2000)
    assert await writer.flush(timeout=1)

    async with sessions() as session:
        stored = (await session.exec(select(TerminalOutcome))).one()
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
    assert stored.revenue_msats == 1000
    assert [epoch.epoch for epoch in epochs] == [0, 1]
    assert epochs[0].coverage_end_day == day - timedelta(days=1)
    assert epochs[1].coverage_start_day == day + timedelta(days=1)
    assert epochs[1].current_slot == 1
    assert await writer.stop(timeout=1)


async def test_temporary_insert_failure_retries_retained_item_without_rotation(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = ledger
    day = date(2026, 8, 31)
    timestamp = _timestamp(day)
    writer = TerminalOutcomeWriter(
        session_factory=sessions,
        retry_seconds=0.01,
        heartbeat_seconds=0.05,
        lease_timeout_seconds=1,
        clock=MutableClock(timestamp),
    )
    original = writer._persist_once
    attempts = 0

    async def fail_once(
        queued: outcomes_module._QueuedOutcome,
    ) -> _PersistResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _PersistResult.RETRY
        return await original(queued)

    monkeypatch.setattr(writer, "_persist_once", fail_once)
    monkeypatch.setattr(outcomes_module, "terminal_outcome_writer", writer)

    assert await writer.start()
    _record("request-retry", timestamp)
    assert await writer.flush(timeout=1)
    async with sessions() as session:
        assert len((await session.exec(select(TerminalOutcome))).all()) == 1
        assert len((await session.exec(select(TerminalOutcomeEpoch))).all()) == 1
    assert attempts == 2
    assert await writer.stop(timeout=1)


async def test_queue_overflow_is_nonblocking_and_rotates_epoch(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = ledger
    queued_day = date(2026, 8, 31)
    overflow_day = queued_day + timedelta(days=1)
    clock = MutableClock(_timestamp(queued_day))
    entered = asyncio.Event()
    release = asyncio.Event()
    writer = TerminalOutcomeWriter(
        session_factory=sessions,
        queue_size=1,
        retry_seconds=0.01,
        heartbeat_seconds=0.05,
        lease_timeout_seconds=1,
        clock=clock,
    )

    async def blocked_insert(
        queued: outcomes_module._QueuedOutcome,
    ) -> _PersistResult:
        entered.set()
        await release.wait()
        return _PersistResult.STORED

    monkeypatch.setattr(writer, "_persist_once", blocked_insert)
    monkeypatch.setattr(outcomes_module, "terminal_outcome_writer", writer)

    assert await writer.start()
    _record("request-in-flight", _timestamp(queued_day))
    await asyncio.wait_for(entered.wait(), timeout=1)
    _record("request-queued", _timestamp(queued_day))
    clock.value = _timestamp(overflow_day)
    _record("request-overflow", clock.value)
    assert writer.loss_pending
    release.set()
    assert await writer.flush(timeout=1)
    async with sessions() as session:
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
    assert [epoch.epoch for epoch in epochs] == [0, 1]
    assert epochs[0].coverage_end_day == queued_day - timedelta(days=1)
    assert epochs[1].coverage_start_day == overflow_day + timedelta(days=1)
    assert await writer.stop(timeout=1)


async def test_live_workers_do_not_rotate_and_clean_stop_closes_only_owner(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
) -> None:
    _, sessions = ledger
    timestamp = _timestamp(date(2026, 8, 31))
    first = TerminalOutcomeWriter(
        session_factory=sessions,
        heartbeat_seconds=0.05,
        lease_timeout_seconds=1,
        clock=MutableClock(timestamp),
    )
    second = TerminalOutcomeWriter(
        session_factory=sessions,
        heartbeat_seconds=0.05,
        lease_timeout_seconds=1,
        clock=MutableClock(timestamp),
    )
    assert await first.start()
    assert await second.start()
    async with sessions() as session:
        assert len((await session.exec(select(TerminalOutcomeEpoch))).all()) == 1
        assert len((await session.exec(select(TerminalOutcomeWriterRun))).all()) == 2

    first_run_id = first._run_id
    second_run_id = second._run_id
    assert await first.stop(timeout=1)
    async with sessions() as session:
        first_run = await session.get(TerminalOutcomeWriterRun, first_run_id)
        second_run = await session.get(TerminalOutcomeWriterRun, second_run_id)
    assert first_run is not None and first_run.status == "clean"
    assert second_run is not None and second_run.status == "active"
    assert await second.stop(timeout=1)


async def test_worker_that_starts_collecting_late_voids_the_day_it_missed(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
) -> None:
    _, sessions = ledger
    day = date(2026, 8, 31)
    first = TerminalOutcomeWriter(
        session_factory=sessions, clock=MutableClock(_timestamp(day, 23))
    )
    # Another worker learns of the opt-in just after midnight, while serving.
    late = TerminalOutcomeWriter(
        session_factory=sessions,
        lease_timeout_seconds=7200,
        clock=MutableClock(_timestamp(day + timedelta(days=1), 0)),
    )
    assert await first.start(serving=True)
    assert await late.start(serving=True)
    async with sessions() as session:
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
    assert [(row.coverage_start_day, row.coverage_end_day) for row in epochs] == [
        (day + timedelta(days=1), day),
        (day + timedelta(days=2), None),
    ]
    assert await first.stop(timeout=1)
    assert await late.stop(timeout=1)


async def test_stale_run_rotates_once_with_concurrent_recovery(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
) -> None:
    _, sessions = ledger
    lost_day = date(2026, 8, 30)
    recovery_day = date(2026, 8, 31)
    lost_at = _timestamp(lost_day)
    recovered_at = _timestamp(recovery_day)
    async with sessions() as session:
        session.add(
            TerminalOutcomeEpoch(
                epoch=0,
                coverage_start_day=lost_day + timedelta(days=1),
                current_slot=1,
            )
        )
        session.add(
            TerminalOutcomeWriterRun(
                run_id="lost-run",
                status="lost",
                started_at_ms=lost_at,
                heartbeat_at_ms=lost_at,
                closed_at_ms=lost_at,
                loss_day=lost_day,
            )
        )
        await session.commit()

    first = TerminalOutcomeWriter(
        session_factory=sessions, clock=MutableClock(recovered_at)
    )
    second = TerminalOutcomeWriter(
        session_factory=sessions, clock=MutableClock(recovered_at)
    )
    await asyncio.gather(first._recover_pending_runs(), second._recover_pending_runs())

    async with sessions() as session:
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
        lost_run = await session.get(TerminalOutcomeWriterRun, "lost-run")
    assert [epoch.epoch for epoch in epochs] == [0, 1]
    assert epochs[0].coverage_end_day == lost_day - timedelta(days=1)
    assert epochs[1].coverage_start_day == recovery_day + timedelta(days=1)
    assert lost_run is not None and lost_run.status == "recovered"


async def test_recovery_does_not_claim_a_late_earlier_loss(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = ledger
    selected_day = date(2026, 8, 30)
    late_day = date(2026, 8, 29)
    recovered_at = _timestamp(date(2026, 8, 31))
    async with sessions() as session:
        session.add(
            TerminalOutcomeEpoch(
                epoch=0,
                coverage_start_day=selected_day,
                current_slot=1,
            )
        )
        session.add(
            TerminalOutcomeWriterRun(
                run_id="selected-loss",
                status="lost",
                started_at_ms=_timestamp(selected_day),
                heartbeat_at_ms=_timestamp(selected_day),
                closed_at_ms=_timestamp(selected_day),
                loss_day=selected_day,
            )
        )
        await session.commit()

    writer = TerminalOutcomeWriter(
        session_factory=sessions, clock=MutableClock(recovered_at)
    )
    original_stage_rotation = writer._stage_rotation

    async def inject_late_loss(
        session: AsyncSession,
        current: TerminalOutcomeEpoch,
        lost_day: date,
    ) -> int | None:
        next_epoch = await original_stage_rotation(session, current, lost_day)
        session.add(
            TerminalOutcomeWriterRun(
                run_id="late-loss",
                status="lost",
                started_at_ms=_timestamp(late_day),
                heartbeat_at_ms=_timestamp(late_day),
                closed_at_ms=_timestamp(late_day),
                loss_day=late_day,
            )
        )
        return next_epoch

    monkeypatch.setattr(writer, "_stage_rotation", inject_late_loss)
    await writer._recover_pending_runs()

    async with sessions() as session:
        selected = await session.get(TerminalOutcomeWriterRun, "selected-loss")
        late = await session.get(TerminalOutcomeWriterRun, "late-loss")
    assert selected is not None and selected.status == "recovered"
    assert late is not None and late.status == "lost"


def test_record_wrapper_never_raises_on_invalid_or_failed_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubWriter:
        def __init__(self) -> None:
            self.losses: list[str] = []
            self.submissions: list[outcomes_module._QueuedOutcome] = []

        def declare_loss(self, reason: str, lost_day: date | None = None) -> None:
            self.losses.append(reason)

        def submit(self, outcome: outcomes_module._QueuedOutcome) -> bool:
            self.submissions.append(outcome)
            if outcome.outcome_id == "request-submit-error":
                raise RuntimeError("submission failed")
            return True

    writer = StubWriter()
    monkeypatch.setattr(outcomes_module, "terminal_outcome_writer", writer)
    record_terminal_outcome(
        TerminalOutcomeContext("request-invalid", "author/model"),
        input_tokens=-1,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=0,
    )
    record_terminal_outcome(
        TerminalOutcomeContext("request-unstorable", "author/model"),
        input_tokens=10**30,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=0,
    )
    record_terminal_outcome(
        TerminalOutcomeContext("request-unreadable-usage", "author/model"),
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=105,
        usage={"input_tokens": 100, "input_tokens_details": {"cached_tokens": "1e309"}},
    )
    record_terminal_outcome(
        TerminalOutcomeContext("request-unknown-model", None),
        input_tokens=1,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=0,
    )
    record_terminal_outcome(
        TerminalOutcomeContext("request-submit-error", "author/model"),
        input_tokens=1,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=0,
    )
    assert writer.losses == [
        "invalid settled terminal outcome",
        "invalid settled terminal outcome",
        "terminal outcome submission raised",
        "terminal outcome submission raised",
    ]
    assert writer.submissions[0].model_identifier is None


def test_cashu_retained_msats_uses_exact_persisted_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    losses: list[str] = []
    monkeypatch.setattr(outcomes_module, "mark_terminal_outcome_loss", losses.append)
    assert cashu_retained_msats(10, "sat", 8) == 2000
    assert cashu_retained_msats(2500, "msat", 501) == 1999
    assert cashu_retained_msats(1, "sat", 2) is None
    assert losses == ["invalid Cashu retained value"]


async def test_collection_pause_excludes_disabled_days_after_restart(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
) -> None:
    _, sessions = ledger
    day = date(2026, 9, 1)
    clock = MutableClock(_timestamp(day))
    writer = TerminalOutcomeWriter(session_factory=sessions, clock=clock)
    assert await writer.start()
    clock.value = _timestamp(day + timedelta(days=3))
    assert await writer.stop(timeout=1, close_coverage=True)
    clock.value = _timestamp(day + timedelta(days=6))
    assert await writer.start()
    async with sessions() as session:
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
    assert epochs[0].coverage_start_day == day + timedelta(days=1)
    assert epochs[0].coverage_end_day == day + timedelta(days=2)
    assert epochs[1].coverage_start_day == day + timedelta(days=7)
    assert await writer.stop(timeout=1)


@pytest.mark.parametrize("charge", [0, 1200])
async def test_encrypted_settlement_persists_once_after_successful_debit(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
    charge: int,
) -> None:
    from routstr.auth import get_reservation_snapshot, pay_for_request
    from routstr.core.db import ApiKey
    from routstr.upstream.ehbp import finalize_ehbp_actual_cost_payment

    _, sessions = ledger
    writer = TerminalOutcomeWriter(session_factory=sessions)
    monkeypatch.setattr(outcomes_module, "terminal_outcome_writer", writer)
    monkeypatch.setattr("routstr.upstream.ehbp.ROUTSTR_FEE_PERCENT", 0)
    assert await writer.start()
    async with sessions() as session:
        key = ApiKey(hashed_key=f"settlement-{charge}", balance=10_000)
        session.add(key)
        await session.commit()
        await pay_for_request(key, 3000, session)
        reservation = await get_reservation_snapshot(key, session)
        context = TerminalOutcomeContext(
            outcome_id=f"request-{charge}",
            model_identifier="canonical/model",
            served_model_identifier="provider-model-v2",
        )
        cost_info = {
            "total_msats": charge,
            "input_tokens": 10,
            "output_tokens": 20,
            "input_observed": True,
            "output_observed": True,
            "cache_read_observed": False,
            "cache_creation_observed": False,
            "pricing_source": "configured",
        }
        args = (
            key,
            session,
            3000,
            "provider-model-v2",
            cost_info,
            reservation,
            context,
        )
        assert await finalize_ehbp_actual_cost_payment(*args) == charge
        with pytest.raises(RuntimeError, match="reservation record does not match"):
            await finalize_ehbp_actual_cost_payment(*args)
        assert key.balance == 10_000 - charge
        assert key.reserved_balance == 0
    assert await writer.flush(timeout=1)
    async with sessions() as session:
        stored = (await session.exec(select(TerminalOutcome))).one()
    assert stored.revenue_msats == charge
    assert stored.input_tokens == 10 and stored.output_tokens == 20
    assert stored.model_identifier == "canonical/model"
    assert stored.served_model_identifier == "provider-model-v2"
    assert stored.input_source == stored.output_source == "reported"
    assert stored.cache_read_source == stored.cache_creation_source == "missing"
    assert stored.pricing_source == "configured"
    assert await writer.stop(timeout=1)


async def test_writer_failure_does_not_fail_settlement_and_marks_gap(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr.auth import get_reservation_snapshot, pay_for_request
    from routstr.core.db import ApiKey
    from routstr.upstream.ehbp import finalize_ehbp_actual_cost_payment

    _, sessions = ledger
    clock = MutableClock(_timestamp(date(2026, 9, 18)))
    writer = TerminalOutcomeWriter(session_factory=sessions, clock=clock)
    monkeypatch.setattr(outcomes_module, "terminal_outcome_writer", writer)
    monkeypatch.setattr("routstr.upstream.ehbp.ROUTSTR_FEE_PERCENT", 0)
    assert await writer.start()

    def unavailable(_: outcomes_module._QueuedOutcome) -> bool:
        raise OSError("synthetic stats storage failure")

    monkeypatch.setattr(writer, "submit", unavailable)
    async with sessions() as session:
        key = ApiKey(hashed_key="settlement-during-stats-failure", balance=10_000)
        session.add(key)
        await session.commit()
        await pay_for_request(key, 3000, session)
        reservation = await get_reservation_snapshot(key, session)
        charged = await finalize_ehbp_actual_cost_payment(
            key,
            session,
            3000,
            "model",
            {"total_msats": 1200},
            reservation,
            TerminalOutcomeContext("request-storage-failure", "model"),
        )
        assert charged == 1200
        assert key.balance == 8800 and key.reserved_balance == 0
    assert await writer.flush()
    async with sessions() as session:
        assert not (await session.exec(select(TerminalOutcome))).all()
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
    assert len(epochs) == 2
    assert epochs[0].coverage_end_day == date(2026, 9, 17)
    assert epochs[1].coverage_start_day == date(2026, 9, 19)
    assert await writer.stop(timeout=1)


async def test_unclean_restart_preserves_days_before_last_durable_flush(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
) -> None:
    _, sessions = ledger
    first_day = date(2026, 9, 1)
    last_day = date(2026, 9, 10)
    clock = MutableClock(_timestamp(first_day))
    writer = TerminalOutcomeWriter(
        session_factory=sessions,
        clock=clock,
        heartbeat_seconds=0.05,
        lease_timeout_seconds=1,
    )
    assert await writer.start()
    clock.value = _timestamp(last_day)
    assert await writer.flush(timeout=1)
    assert writer._task is not None
    writer._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer._task
    assert not await writer.stop(timeout=1)
    clock.value += 2000
    assert await writer.start()
    async with sessions() as session:
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
    try:
        assert epochs[0].coverage_end_day == last_day - timedelta(days=1)
        assert epochs[1].coverage_start_day == last_day + timedelta(days=1)
    finally:
        assert await writer.stop(timeout=1)


async def test_sharing_disabled_startup_resumes_private_coverage_after_gap(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nostr_sdk import Keys

    from routstr.nostr import analytics_runtime as runtime

    _, sessions = ledger
    day = date(2026, 9, 1)
    clock = MutableClock(_timestamp(day))
    writer = TerminalOutcomeWriter(session_factory=sessions, clock=clock)
    assert await writer.start()
    await runtime.claim_analytics_v2_identity(
        sessions,
        pubkey=Keys.parse("11" * 32).public_key().to_hex(),
        provider_d="provider",
        at_ms=clock.value,
    )
    await runtime.activate_analytics_v2_sharing(
        sessions, coverage_day=day, at_ms=clock.value
    )
    clock.value = _timestamp(day + timedelta(days=2))
    assert await writer.flush(timeout=1)
    assert await writer.stop(timeout=1)
    clock.value = _timestamp(day + timedelta(days=5))
    stopped_writer = TerminalOutcomeWriter(session_factory=sessions, clock=clock)
    monkeypatch.setattr(outcomes_module, "terminal_outcome_writer", stopped_writer)
    monkeypatch.setattr(runtime, "terminal_outcome_writer", stopped_writer)
    monkeypatch.setattr(runtime, "create_session", sessions)
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", False)
    coordinator = runtime.AnalyticsCoordinator()
    await coordinator.prepare_startup()
    assert stopped_writer.running
    await coordinator.close()
    assert not (await runtime.get_analytics_v2_delivery_state(sessions)).sharing_enabled
    async with sessions() as session:
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
    assert all(epoch.current_slot is None for epoch in epochs[:-1])
    assert epochs[0].coverage_end_day == day + timedelta(days=1)
    assert all(
        epoch.coverage_end_day is not None
        and epoch.coverage_start_day > epoch.coverage_end_day
        for epoch in epochs[1:-1]
    )
    assert epochs[-1].current_slot == 1
    assert epochs[-1].coverage_start_day == day + timedelta(days=6)
    assert epochs[-1].coverage_end_day is None


@pytest.mark.parametrize("fresh_process", [False, True])
async def test_failed_writer_start_cannot_backfill_missed_days_as_zero(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
    fresh_process: bool,
) -> None:
    _, sessions = ledger
    day = date(2026, 9, 1)
    clock = MutableClock(_timestamp(day))
    writer = TerminalOutcomeWriter(session_factory=sessions, clock=clock)
    original = writer._create_run

    async def unavailable(*args: object) -> None:
        raise OSError("synthetic stats startup failure")

    monkeypatch.setattr(writer, "_create_run", unavailable)
    assert not await writer.start()
    assert writer.loss_pending
    monkeypatch.setattr(writer, "_create_run", original)
    if fresh_process:
        writer = TerminalOutcomeWriter(session_factory=sessions, clock=clock)
    clock.value = _timestamp(day + timedelta(days=3))
    assert await writer.start()
    async with sessions() as session:
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
    assert epochs[0].coverage_end_day == (
        day if fresh_process else day - timedelta(days=1)
    )
    assert epochs[1].coverage_start_day == day + timedelta(days=4)
    assert await writer.stop(timeout=1)


async def test_disable_closes_coverage_after_background_rotation_is_stopped(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = ledger
    writer = TerminalOutcomeWriter(session_factory=sessions)
    assert await writer.start()
    original = writer._close_coverage

    async def close_without_racing_writer(day: date) -> None:
        assert not writer.running
        await original(day)

    monkeypatch.setattr(writer, "_close_coverage", close_without_racing_writer)
    assert await writer.stop(timeout=1, close_coverage=True)


@pytest.mark.parametrize("failed_startup", [False, True])
async def test_fresh_restart_does_not_publish_zero_for_unattended_days(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
    failed_startup: bool,
) -> None:
    import json

    from nostr_sdk import Keys

    from routstr.core.db import AnalyticsV2Outbox
    from routstr.nostr.analytics_v2_delivery import (
        AnalyticsV2Producer,
        activate_analytics_v2_sharing,
        claim_analytics_v2_identity,
    )

    _, sessions = ledger
    first_day, clean_day, recovery_day = (
        date(2026, 9, 1),
        date(2026, 9, 3),
        date(2026, 9, 7),
    )
    clock = MutableClock(_timestamp(first_day))
    pubkey = Keys.parse("11" * 32).public_key().to_hex()
    await claim_analytics_v2_identity(
        sessions, pubkey=pubkey, provider_d="provider", at_ms=clock.value
    )
    await activate_analytics_v2_sharing(
        sessions, coverage_day=first_day, at_ms=clock.value
    )
    first = TerminalOutcomeWriter(session_factory=sessions, clock=clock)
    assert await first.start()
    clock.value = _timestamp(clean_day)
    assert await first.flush(timeout=1)
    assert await first.stop(timeout=1)

    if failed_startup:

        @asynccontextmanager
        async def unavailable() -> AsyncGenerator[AsyncSession, None]:
            raise OSError("synthetic stats database unavailable at startup")
            yield  # pragma: no cover

        failed = TerminalOutcomeWriter(
            session_factory=unavailable,
            clock=MutableClock(_timestamp(clean_day + timedelta(days=1))),
        )
        assert not await failed.start()
        assert failed.loss_pending

    clock.value = _timestamp(recovery_day)
    recovered = TerminalOutcomeWriter(session_factory=sessions, clock=clock)
    assert await recovered.start()
    producer = AnalyticsV2Producer(
        sessions,
        private_key_hex="11" * 32,
        public_key_hex=pubkey,
        provider_d="provider",
    )
    try:
        assert (
            await producer.produce_once(
                now=datetime.fromtimestamp(clock.value / 1000, UTC)
            )
            == 1
        )
        async with sessions() as session:
            reports = (await session.exec(select(AnalyticsV2Outbox))).all()
        reported_days = {
            day
            for report in reports
            for day in json.loads(json.loads(report.frame)[1]["content"])["days"]
        }
        assert reported_days == {"2026-09-02"}
    finally:
        assert await recovered.stop(timeout=1)


async def test_restart_alongside_live_writer_preserves_continuous_coverage(
    ledger: tuple[AsyncEngine, outcomes_module.SessionFactory],
) -> None:
    _, sessions = ledger
    first_day = date(2026, 9, 1)
    clock = MutableClock(_timestamp(first_day))
    writers = [
        TerminalOutcomeWriter(session_factory=sessions, clock=clock) for _ in range(3)
    ]
    assert await writers[0].start()
    assert await writers[1].start()
    clock.value = _timestamp(first_day + timedelta(days=3))
    assert await writers[0].flush(timeout=1)
    assert await writers[1].flush(timeout=1)
    assert await writers[0].stop(timeout=1)
    assert await writers[2].start()
    try:
        async with sessions() as session:
            epochs = (await session.exec(select(TerminalOutcomeEpoch))).all()
        assert len(epochs) == 1
        assert epochs[0].coverage_start_day == first_day + timedelta(days=1)
        assert epochs[0].coverage_end_day is None
    finally:
        assert await writers[1].stop(timeout=1)
        assert await writers[2].stop(timeout=1)
