from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import (
    AnalyticsV2Outbox,
    TerminalOutcome,
    TerminalOutcomeEpoch,
)
from routstr.nostr.analytics_v2 import aggregate_ledger_week, encode_week_event
from routstr.nostr.analytics_v2_delivery import (
    AnalyticsV2Delivery,
    AnalyticsV2Producer,
    RelaySendResult,
    RelayTarget,
    activate_analytics_v2_sharing,
    claim_analytics_v2_identity,
    enqueue_signed_event,
    run_analytics_v2_publisher,
    transition_analytics_v2_sharing,
)

PRIVATE_KEY = "11" * 32
PUBLIC_KEY = Keys.parse(PRIVATE_KEY).public_key().to_hex()
WEEK = date(2026, 8, 31)


def _at_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


@pytest_asyncio.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'producer.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _outcome(
    outcome_id: str, terminal_day: date, *, tokens: int = 1
) -> TerminalOutcome:
    return TerminalOutcome(
        outcome_id=outcome_id,
        terminal_at_ms=1,
        terminal_day=terminal_day,
        model_identifier="model/served",
        input_observed=True,
        output_observed=True,
        cache_read_observed=None,
        cache_creation_observed=None,
        input_tokens=tokens,
        output_tokens=tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=tokens,
        input_source="reported",
        output_source="reported",
        cache_read_source="missing",
        cache_creation_source="missing",
    )


async def _activate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await claim_analytics_v2_identity(
        factory,
        pubkey=PUBLIC_KEY,
        provider_d="provider",
        at_ms=1,
    )
    await activate_analytics_v2_sharing(
        factory,
        coverage_day=WEEK - timedelta(days=1),
        at_ms=2,
    )


@pytest.mark.asyncio
async def test_producer_recovers_each_current_epoch_week_once_and_corrects_sent_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        session.add(_outcome("old-epoch", WEEK - timedelta(days=2)))
        session.add(_outcome("week-one", WEEK))
        session.add(_outcome("week-two", WEEK + timedelta(days=8), tokens=2))
        await session.commit()

    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    recovery_time = datetime(2026, 9, 14, 0, 1, tzinfo=UTC)
    assert await producer.produce_once(now=recovery_time) == 2
    assert await producer.produce_once(now=recovery_time) == 0

    async with session_factory() as session:
        result = await session.exec(
            select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.week))
        )
        initial = result.all()
    assert [(row.week, row.epoch, row.finalized) for row in initial] == [
        (WEEK, 0, True),
        (WEEK + timedelta(days=7), 0, True),
    ]
    first_payload = json.loads(json.loads(initial[0].frame)[1]["content"])
    second_payload = json.loads(json.loads(initial[1].frame)[1]["content"])
    assert first_payload["days"][WEEK.isoformat()][0] == 1
    assert second_payload["days"][(WEEK + timedelta(days=8)).isoformat()][0] == 1

    async with session_factory() as session:
        first = await session.get(AnalyticsV2Outbox, initial[0].event_id)
        assert first is not None
        first.first_send_attempt_at_ms = 3
        session.add(_outcome("late-week-one", WEEK + timedelta(days=1), tokens=3))
        await session.commit()

    assert await producer.produce_once(now=recovery_time) == 1
    async with session_factory() as session:
        result = await session.exec(
            select(AnalyticsV2Outbox)
            .where(col(AnalyticsV2Outbox.week) == WEEK)
            .order_by(col(AnalyticsV2Outbox.created_at))
        )
        versions = result.all()
    assert len(versions) == 2
    correction_payload = json.loads(json.loads(versions[1].frame)[1]["content"])
    assert correction_payload["corrects"] == versions[0].event_id
    assert correction_payload["corrected"] is True
    assert correction_payload["days"][(WEEK + timedelta(days=1)).isoformat()][0] == 1

    async with session_factory() as session:
        session.add(_outcome("later-week-one", WEEK + timedelta(days=1), tokens=4))
        await session.commit()
    assert await producer.produce_once(now=recovery_time) == 1
    async with session_factory() as session:
        result = await session.exec(
            select(AnalyticsV2Outbox)
            .where(col(AnalyticsV2Outbox.week) == WEEK)
            .order_by(col(AnalyticsV2Outbox.created_at))
        )
        versions = result.all()
    assert [row.status for row in versions] == ["superseded", "superseded", "pending"]
    replacement_payload = json.loads(json.loads(versions[2].frame)[1]["content"])
    assert replacement_payload["corrects"] == versions[0].event_id
    assert replacement_payload["days"][(WEEK + timedelta(days=1)).isoformat()][0] == 2


@pytest.mark.asyncio
async def test_unsent_changed_version_is_coalesced_without_public_correction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        session.add(_outcome("first", WEEK))
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    recovery_time = datetime(2026, 9, 7, 0, 1, tzinfo=UTC)
    assert await producer.produce_once(now=recovery_time) == 1

    async with session_factory() as session:
        session.add(_outcome("late", WEEK + timedelta(days=1)))
        await session.commit()
    assert await producer.produce_once(now=recovery_time) == 1

    async with session_factory() as session:
        result = await session.exec(
            select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
        )
        rows = result.all()
    assert [row.status for row in rows] == ["superseded", "pending"]
    latest_payload = json.loads(json.loads(rows[1].frame)[1]["content"])
    assert "corrects" not in latest_payload
    assert "corrected" not in latest_payload


@pytest.mark.asyncio
async def test_loss_rotation_finalizes_closed_epoch_before_new_epoch_same_week(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        session.add(_outcome("initial", WEEK))
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    assert await producer.produce_once(now=datetime(2026, 9, 2, 0, 1, tzinfo=UTC)) == 1

    async with session_factory() as session:
        old_epoch = await session.get(TerminalOutcomeEpoch, 0)
        assert old_epoch is not None
        old_epoch.coverage_end_day = WEEK + timedelta(days=2)
        old_epoch.current_slot = None
        session.add(
            TerminalOutcomeEpoch(
                epoch=1,
                coverage_start_day=WEEK + timedelta(days=4),
                current_slot=1,
            )
        )
        session.add(_outcome("safe-old", WEEK + timedelta(days=2)))
        session.add(_outcome("new-epoch", WEEK + timedelta(days=4)))
        await session.commit()

    concurrent = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    results = await asyncio.gather(
        producer.produce_once(now=datetime(2026, 9, 6, 0, 1, tzinfo=UTC)),
        concurrent.produce_once(now=datetime(2026, 9, 6, 0, 2, tzinfo=UTC)),
    )
    assert sum(results) == 2
    async with session_factory() as session:
        rows = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
            )
        ).all()
    pending = [row for row in rows if row.status == "pending"]
    assert [(row.epoch, row.finalized) for row in pending] == [
        (0, True),
        (1, False),
    ]
    assert pending[0].d_tag != pending[1].d_tag
    old_payload = json.loads(json.loads(pending[0].frame)[1]["content"])
    assert old_payload["days"][(WEEK + timedelta(days=2)).isoformat()][0] == 1


@pytest.mark.asyncio
async def test_late_change_after_unsent_ordinary_corrects_attempted_predecessor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        session.add(_outcome("first", WEEK))
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    assert await producer.produce_once(now=datetime(2026, 9, 1, 0, 1, tzinfo=UTC)) == 1
    async with session_factory() as session:
        first = (await session.exec(select(AnalyticsV2Outbox))).one()
        first.first_send_attempt_at_ms = 3
        await session.commit()

    next_day = datetime(2026, 9, 2, 0, 1, tzinfo=UTC)
    assert await producer.produce_once(now=next_day) == 1
    async with session_factory() as session:
        session.add(_outcome("late-first-day", WEEK, tokens=2))
        await session.commit()
    assert await producer.produce_once(now=next_day) == 1

    async with session_factory() as session:
        rows = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
            )
        ).all()
    assert [row.status for row in rows] == ["superseded", "superseded", "pending"]
    payload = json.loads(json.loads(rows[2].frame)[1]["content"])
    assert payload["corrects"] == rows[0].event_id
    assert payload["days"][WEEK.isoformat()][0] == 2


@pytest.mark.asyncio
async def test_concurrent_producers_commit_one_ordinary_semantic_slot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        session.add(_outcome("first", WEEK))
        await session.commit()
    first_producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    second_producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )

    results = await asyncio.gather(
        first_producer.produce_once(now=datetime(2026, 9, 1, 0, 1, tzinfo=UTC)),
        second_producer.produce_once(now=datetime(2026, 9, 1, 0, 2, tzinfo=UTC)),
    )
    assert sorted(results) == [0, 1]
    async with session_factory() as session:
        rows = (await session.exec(select(AnalyticsV2Outbox))).all()
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].semantic_slot == f"ordinary:0:{WEEK.isoformat()}"


@pytest.mark.asyncio
async def test_new_epoch_uses_a_distinct_coordinate_from_prior_epoch_same_week(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    aggregate = aggregate_ledger_week(
        [],
        epoch=0,
        epoch_coverage_start=WEEK,
        epoch_coverage_end=None,
        week=WEEK,
        today_utc=WEEK + timedelta(days=2),
    )
    assert aggregate is not None
    prior_created_at = 4_000_000_000
    prior = encode_week_event(
        aggregate,
        private_key_hex=PRIVATE_KEY,
        provider_d="provider",
        created_at=prior_created_at,
    )
    await enqueue_signed_event(session_factory, prior, stored_at_ms=3)

    await transition_analytics_v2_sharing(
        session_factory,
        enabled=False,
        at_ms=_at_ms(WEEK + timedelta(days=2)),
    )
    await activate_analytics_v2_sharing(
        session_factory,
        coverage_day=WEEK + timedelta(days=3),
        at_ms=5,
    )
    async with session_factory() as session:
        session.add(_outcome("new-epoch", WEEK + timedelta(days=4)))
        await session.commit()

    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    assert await producer.produce_once(now=datetime(2026, 9, 6, 0, 1, tzinfo=UTC)) == 1
    async with session_factory() as session:
        rows = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
            )
        ).all()
    assert {row.epoch for row in rows} == {0, 1}
    assert len({row.d_tag for row in rows}) == 2
    latest = next(row for row in rows if row.epoch == 1)
    payload = json.loads(json.loads(latest.frame)[1]["content"])
    assert "corrects" not in payload


@pytest.mark.asyncio
async def test_multi_day_disable_excludes_gap_and_never_replays_old_epoch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        session.add(_outcome("before-disable", WEEK))
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    assert await producer.produce_once(now=datetime(2026, 9, 2, 0, 1, tzinfo=UTC)) == 1

    await transition_analytics_v2_sharing(
        session_factory,
        enabled=False,
        at_ms=_at_ms(WEEK + timedelta(days=2)),
    )
    reenable_day = WEEK + timedelta(days=4)
    async with session_factory() as session:
        for offset in (2, 3, 4):
            session.add(_outcome(f"gap-{offset}", WEEK + timedelta(days=offset)))
        session.add(_outcome("after-enable", WEEK + timedelta(days=5)))
        await session.commit()
    activation = await activate_analytics_v2_sharing(
        session_factory,
        coverage_day=reenable_day,
        at_ms=_at_ms(reenable_day),
    )
    assert activation.state.active_epoch_floor == 1

    assert await producer.produce_once(now=datetime(2026, 9, 7, 0, 1, tzinfo=UTC)) == 1
    async with session_factory() as session:
        epochs = (
            await session.exec(
                select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
            )
        ).all()
        rows = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
            )
        ).all()
    assert [
        (epoch.epoch, epoch.coverage_start_day, epoch.coverage_end_day)
        for epoch in epochs
    ] == [
        # Private coverage continued while public sharing was off.
        (0, WEEK, WEEK + timedelta(days=3)),
        (1, WEEK + timedelta(days=5), None),
    ]
    assert [(row.epoch, row.status) for row in rows] == [
        (0, "cancelled"),
        (1, "pending"),
    ]
    payload = json.loads(json.loads(rows[1].frame)[1]["content"])
    assert set(payload["days"]) == {
        (WEEK + timedelta(days=5)).isoformat(),
        (WEEK + timedelta(days=6)).isoformat(),
    }


@pytest.mark.asyncio
async def test_publisher_loop_performs_no_production_or_send_while_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sends = 0

    async def sender(
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        nonlocal sends
        sends += 1
        return RelaySendResult(True, True)

    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    delivery = AnalyticsV2Delivery(
        session_factory, operator_relays=("wss://relay.valid.net",), sender=sender
    )
    task = asyncio.create_task(
        run_analytics_v2_publisher(producer, delivery, interval_seconds=0.01)
    )
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with session_factory() as session:
        result = await session.exec(select(AnalyticsV2Outbox))
        assert result.all() == []
    assert sends == 0


@pytest.mark.asyncio
async def test_model_only_correction_keeps_totals_and_updates_daily_partition(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        session.add(_outcome("first", WEEK))
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    now = datetime(2026, 9, 7, 0, 1, tzinfo=UTC)
    assert await producer.produce_once(now=now) == 1
    async with session_factory() as session:
        original = (await session.exec(select(AnalyticsV2Outbox))).one()
        original.first_send_attempt_at_ms = 3
        outcome = await session.get(TerminalOutcome, "first")
        assert outcome is not None
        outcome.model_identifier = "model/corrected"
        await session.commit()
    assert await producer.produce_once(now=now) == 1
    assert await producer.produce_once(now=now) == 0
    async with session_factory() as session:
        rows = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
            )
        ).all()
    before, after = [json.loads(json.loads(row.frame)[1]["content"]) for row in rows]
    assert before["days"] == after["days"]
    assert after["corrects"] == original.event_id
    assert "model/corrected" in after["daily_models"][WEEK.isoformat()]


@pytest.mark.asyncio
async def test_database_groups_requests_without_losing_usage_provenance(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        for index in range(40):
            session.add(_outcome(str(index), WEEK, tokens=index + 1))
        estimated = _outcome("estimated", WEEK, tokens=5)
        estimated.input_observed = False
        estimated.input_source = "estimated"
        session.add(estimated)
        missing = _outcome("missing", WEEK, tokens=0)
        missing.input_observed = None
        missing.output_observed = None
        missing.input_source = "missing"
        missing.output_source = "missing"
        session.add(missing)
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    _, grouped = await producer._load_eligible_epochs(0, WEEK + timedelta(days=1))
    assert len(grouped) == 3
    assert sum(row.completed_requests for row in grouped) == 42
    assert await producer.produce_once(now=datetime(2026, 9, 1, 0, 1, tzinfo=UTC)) == 1
    async with session_factory() as session:
        stored = (await session.exec(select(AnalyticsV2Outbox))).one()
    payload = json.loads(json.loads(stored.frame)[1]["content"])
    values = payload["days"][WEEK.isoformat()]
    assert values[0] == 42
    assert values[1] == 40
    assert values[5] == sum(range(1, 41)) + 5
    assert values[9] == sum(range(1, 41)) + 5
    assert values[10] == 1
    assert values[14] == 1
    assert values[18:] == [40, 2 * sum(range(1, 41))]


@pytest.mark.asyncio
async def test_database_grouping_keeps_measured_cache_cohort_separate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    rows = [_outcome("plain-a", WEEK, tokens=10), _outcome("plain-b", WEEK, tokens=20)]
    rows.append(_outcome("measured-zero", WEEK, tokens=0))
    cached = _outcome("cached", WEEK, tokens=30)
    cached.cache_read_source = cached.cache_creation_source = "reported"
    cached.cache_read_input_tokens = 8
    cached.cache_creation_input_tokens = 2
    rows.append(cached)
    for side in ("cache_read", "cache_creation"):
        missing_cache = _outcome(side, WEEK, tokens=100)
        setattr(missing_cache, f"{side}_input_tokens", 5)
        rows.append(missing_cache)
    estimated = _outcome("estimated", WEEK, tokens=40)
    estimated.input_source = "estimated"
    rows.append(estimated)
    input_only = _outcome("input-only", WEEK, tokens=50)
    input_only.output_source = "missing"
    input_only.output_observed = False
    input_only.output_tokens = 0
    rows.append(input_only)
    output_only = _outcome("output-only", WEEK, tokens=60)
    output_only.input_source = "missing"
    output_only.input_observed = False
    output_only.input_tokens = 0
    rows.append(output_only)
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    _, grouped = await producer._load_eligible_epochs(0, WEEK + timedelta(days=1))
    assert len(grouped) == 6
    assert sum(row.completed_requests for row in grouped) == 9
    assert await producer.produce_once(now=datetime(2026, 9, 1, 0, 1, tzinfo=UTC)) == 1
    async with session_factory() as session:
        stored = (await session.exec(select(AnalyticsV2Outbox))).one()
    payload = json.loads(json.loads(stored.frame)[1]["content"])
    values = payload["days"][WEEK.isoformat()]
    assert values[0] == 9
    assert values[18:] == [4, 130]
    assert payload["daily_models"][WEEK.isoformat()]["model/served"] == values


@pytest.mark.asyncio
async def test_database_normalization_preserves_estimates_and_observed_only_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        for source in ("reported", "estimated", "missing"):
            for observed in (None, False, True):
                row = _outcome(f"{source}/{observed}", WEEK, tokens=10)
                row.model_identifier = row.outcome_id
                row.cache_read_input_tokens = 6
                row.cache_creation_input_tokens = 4
                for name in ("input", "output", "cache_read", "cache_creation"):
                    setattr(row, f"{name}_source", source)
                    setattr(row, f"{name}_observed", observed)
                session.add(row)
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    assert await producer.produce_once(now=datetime(2026, 9, 1, 0, 1, tzinfo=UTC)) == 1
    async with session_factory() as session:
        stored = (await session.exec(select(AnalyticsV2Outbox))).one()
    payload = json.loads(json.loads(stored.frame)[1]["content"])
    models = payload["daily_models"][WEEK.isoformat()]
    for source in ("reported", "estimated", "missing"):
        for observed in (None, False, True):
            values = models[f"{source}/{observed}"]
            if source == "estimated":
                assert values[10:14] == [1] * 4
                assert values[18:] == [0, 0]
            elif source == "reported" or observed is True:
                assert values[1:5] == [1] * 4
                assert values[18:] == [1, 30]
            else:
                assert values[14:18] == [1] * 4
                assert values[18:] == [0, 0]
    assert payload["days"][WEEK.isoformat()][18:] == [4, 120]


@pytest.mark.asyncio
async def test_measured_cohort_change_corrects_an_otherwise_identical_report(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    measured = _outcome("measured", WEEK, tokens=10)
    missing = _outcome("missing", WEEK, tokens=0)
    missing.input_source = missing.output_source = "missing"
    missing.input_observed = missing.output_observed = False
    async with session_factory() as session:
        session.add_all([measured, missing])
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    now = datetime(2026, 9, 1, 0, 1, tzinfo=UTC)
    assert await producer.produce_once(now=now) == 1
    async with session_factory() as session:
        original = (await session.exec(select(AnalyticsV2Outbox))).one()
        original.first_send_attempt_at_ms = 3
        input_only = await session.get(TerminalOutcome, "measured")
        output_only = await session.get(TerminalOutcome, "missing")
        assert input_only is not None and output_only is not None
        input_only.output_observed = False
        input_only.output_source = "missing"
        input_only.output_tokens = 0
        output_only.output_observed = True
        output_only.output_source = "reported"
        output_only.output_tokens = 10
        await session.commit()
    assert await producer.produce_once(now=now) == 1
    assert await producer.produce_once(now=now) == 0
    async with session_factory() as session:
        versions = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
            )
        ).all()
    before, after = [
        json.loads(json.loads(row.frame)[1]["content"]) for row in versions
    ]
    assert before["days"][WEEK.isoformat()][:18] == after["days"][WEEK.isoformat()][:18]
    assert before["days"][WEEK.isoformat()][18:] == [1, 20]
    assert after["days"][WEEK.isoformat()][18:] == [0, 0]
    assert after["corrects"] == versions[0].event_id


@pytest.mark.asyncio
async def test_history_limit_preserves_the_oldest_whole_week(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    now = datetime(2027, 9, 3, 0, 1, tzinfo=UTC)
    cutoff = now.date() - timedelta(days=365)
    monday = cutoff - timedelta(days=cutoff.weekday())
    assert monday < cutoff
    async with session_factory() as session:
        session.add(_outcome("before-window-within-week", monday, tokens=9))
        session.add(_outcome("inside-window", cutoff, tokens=3))
        session.add(_outcome("too-old", monday - timedelta(days=1), tokens=100))
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    assert await producer.produce_once(now=now) > 0
    assert await producer.produce_once(now=now) == 0
    async with session_factory() as session:
        rows = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.week))
            )
        ).all()
    assert rows[0].week == monday
    payload = json.loads(json.loads(rows[0].frame)[1]["content"])
    assert payload["coverage_start"] == monday.isoformat()
    assert payload["days"][monday.isoformat()][9] == 9
    assert payload["days"][cutoff.isoformat()][9] == 3


@pytest.mark.asyncio
async def test_private_writer_continues_through_public_sharing_toggles(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routstr.core import terminal_outcomes as ledger

    clock = [_at_ms(WEEK - timedelta(days=1))]
    writer = ledger.TerminalOutcomeWriter(
        session_factory=session_factory,
        clock=lambda: clock[0],
        heartbeat_seconds=0.01,
        lease_timeout_seconds=100,
    )
    monkeypatch.setattr(ledger, "terminal_outcome_writer", writer)
    assert await writer.start()
    try:
        await _activate(session_factory)
        clock[0] = _at_ms(WEEK)
        ledger.record_terminal_outcome(
            ledger.TerminalOutcomeContext("before", "model/served"),
            input_tokens=1,
            output_tokens=1,
            revenue_msats=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            terminal_at_ms=clock[0],
        )
        assert await writer.flush()
        clock[0] = _at_ms(WEEK + timedelta(days=1))
        await transition_analytics_v2_sharing(
            session_factory, enabled=False, at_ms=clock[0]
        )
        assert writer.running
        ledger.record_terminal_outcome(
            ledger.TerminalOutcomeContext("private", "model/served"),
            input_tokens=2,
            output_tokens=2,
            revenue_msats=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            terminal_at_ms=clock[0],
        )
        assert await writer.flush()
        clock[0] = _at_ms(WEEK + timedelta(days=2))
        activation = await activate_analytics_v2_sharing(
            session_factory,
            coverage_day=WEEK + timedelta(days=2),
            at_ms=clock[0],
        )
        assert activation.transitioned
        assert writer.running
        ledger.record_terminal_outcome(
            ledger.TerminalOutcomeContext("after", "model/served"),
            input_tokens=3,
            output_tokens=3,
            revenue_msats=3,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            terminal_at_ms=clock[0],
        )
        assert await writer.flush()
        async with session_factory() as session:
            rows = (await session.exec(select(TerminalOutcome))).all()
        assert {row.outcome_id for row in rows} == {"before", "private", "after"}
        assert sum(row.revenue_msats for row in rows) == 6
    finally:
        assert await writer.stop()


@pytest.mark.asyncio
async def test_midnight_publication_waits_for_every_writer_to_drain(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from routstr.core.db import TerminalOutcomeWriterRun

    await _activate(session_factory)
    day_after = WEEK + timedelta(days=2)
    async with session_factory() as session:
        session.add(_outcome("saved-first-day", WEEK))
        session.add(
            TerminalOutcomeWriterRun(
                run_id="other-worker",
                status="active",
                started_at_ms=_at_ms(WEEK),
                heartbeat_at_ms=_at_ms(day_after),
                flushed_through_ms=_at_ms(WEEK + timedelta(days=1)),
            )
        )
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    now = datetime(2026, 9, 2, 0, 1, tzinfo=UTC)
    assert await producer.produce_once(now=now) == 1
    async with session_factory() as session:
        prior = (await session.exec(select(AnalyticsV2Outbox))).one()
        payload = json.loads(json.loads(prior.frame)[1]["content"])
        assert payload["through"] == WEEK.isoformat()
        session.add(
            _outcome("queued-before-midnight", WEEK + timedelta(days=1), tokens=50)
        )
        run = await session.get(TerminalOutcomeWriterRun, "other-worker")
        assert run is not None
        run.flushed_through_ms = _at_ms(day_after)
        await session.commit()
    assert await producer.produce_once(now=now) == 1
    async with session_factory() as session:
        latest = (
            await session.exec(
                select(AnalyticsV2Outbox).where(
                    col(AnalyticsV2Outbox.status) == "pending"
                )
            )
        ).one()
    payload = json.loads(json.loads(latest.frame)[1]["content"])
    assert payload["through"] == (WEEK + timedelta(days=1)).isoformat()
    assert payload["days"][(WEEK + timedelta(days=1)).isoformat()][9] == 50


@pytest.mark.asyncio
async def test_new_relay_size_limit_replaces_oversize_frames_without_mutating_them(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    async with session_factory() as session:
        for index in range(100):
            row = _outcome(str(index), WEEK, tokens=index + 1)
            row.model_identifier = f"model/{index}/" + "x" * 80
            session.add(row)
        await session.commit()
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    now = datetime(2026, 9, 7, 0, 1, tzinfo=UTC)
    assert await producer.produce_once(now=now) == 1
    async with session_factory() as session:
        original = (await session.exec(select(AnalyticsV2Outbox))).one()
        original.first_send_attempt_at_ms = 3
        original_bytes = bytes(original.frame)
        await session.commit()
    limit = len(original_bytes) // 2
    assert await producer.produce_once(now=now, max_frame_bytes=limit) == 1
    assert await producer.produce_once(now=now, max_frame_bytes=limit) == 0
    async with session_factory() as session:
        rows = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
            )
        ).all()
    assert bytes(rows[0].frame) == original_bytes
    assert len(rows[1].frame) <= limit
    before, after = [json.loads(json.loads(row.frame)[1]["content"]) for row in rows]
    assert before["days"] == after["days"]
    assert after["corrects"] == original.event_id
    assert after["daily_models"][WEEK.isoformat()]["_other"][0] > 0

    async with session_factory() as session:
        row = _outcome("second-week", WEEK + timedelta(days=7))
        session.add(row)
        await session.commit()
    assert (
        await producer.produce_once(
            now=datetime(2026, 9, 8, 0, 1, tzinfo=UTC), max_frame_bytes=limit
        )
        == 1
    )
    async with session_factory() as session:
        second_week = (
            await session.exec(
                select(AnalyticsV2Outbox).where(
                    col(AnalyticsV2Outbox.week) == WEEK + timedelta(days=7)
                )
            )
        ).one()
    assert len(second_week.frame) <= limit


@pytest.mark.asyncio
async def test_epoch_closure_finalizes_existing_last_day_without_new_requests(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    producer = AnalyticsV2Producer(
        session_factory,
        private_key_hex=PRIVATE_KEY,
        public_key_hex=PUBLIC_KEY,
        provider_d="provider",
    )
    now = datetime(2026, 9, 2, 0, 1, tzinfo=UTC)
    assert await producer.produce_once(now=now) == 1
    async with session_factory() as session:
        first = (await session.exec(select(AnalyticsV2Outbox))).one()
        first.first_send_attempt_at_ms = 3
        epoch = await session.get(TerminalOutcomeEpoch, 0)
        assert epoch is not None
        epoch.coverage_end_day = WEEK + timedelta(days=1)
        epoch.current_slot = None
        await session.commit()
    assert await producer.produce_once(now=now) == 1
    assert await producer.produce_once(now=now) == 0
    async with session_factory() as session:
        rows = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
            )
        ).all()
    assert rows[-1].finalized
    assert rows[-1].through_day == rows[0].through_day
