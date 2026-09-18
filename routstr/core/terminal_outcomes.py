from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import AsyncContextManager

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from .db import (
    TerminalOutcome,
    TerminalOutcomeEpoch,
    TerminalOutcomeWriterRun,
    create_session,
)
from .logging import get_logger

logger = get_logger(__name__)

_QUEUE_SIZE = 4096
_RETRY_SECONDS = 1.0
_HEARTBEAT_SECONDS = 15.0
_LEASE_TIMEOUT_SECONDS = 90.0
# Far above any real request, and low enough that a day's sums stay JSON-safe.
_MAX_TOKENS = 2**31 - 1
_MAX_REVENUE_MSATS = 2**40

SessionFactory = Callable[[], AsyncContextManager[AsyncSession]]
Clock = Callable[[], int]


@dataclass(frozen=True)
class TerminalOutcomeContext:
    outcome_id: str | None
    model_identifier: str | None
    input_observed: bool | None = None
    output_observed: bool | None = None
    cache_read_observed: bool | None = None
    cache_creation_observed: bool | None = None

    served_model_identifier: str | None = None
    pricing_source: str | None = None
    input_source: str | None = None
    output_source: str | None = None
    cache_read_source: str | None = None
    cache_creation_source: str | None = None


@dataclass(frozen=True)
class _QueuedOutcome:
    outcome_id: str
    terminal_at_ms: int
    terminal_day: date
    model_identifier: str | None
    served_model_identifier: str | None
    pricing_source: str | None
    input_source: str
    output_source: str
    cache_read_source: str
    cache_creation_source: str
    input_observed: bool | None
    output_observed: bool | None
    cache_read_observed: bool | None
    cache_creation_observed: bool | None
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    revenue_msats: int


class _PersistResult(Enum):
    STORED = "stored"
    CONFLICT = "conflict"
    RETRY = "retry"


def _same_outcome(existing: TerminalOutcome, queued: _QueuedOutcome) -> bool:
    return all(
        getattr(existing, field.name) == getattr(queued, field.name)
        for field in fields(queued)
    )


def _utc_day_from_ms(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).date()


async def _current_epoch(session: AsyncSession) -> TerminalOutcomeEpoch | None:
    return (
        await session.exec(
            select(TerminalOutcomeEpoch).where(
                col(TerminalOutcomeEpoch.current_slot) == 1
            )
        )
    ).first()


class TerminalOutcomeWriter:
    def __init__(
        self,
        *,
        session_factory: SessionFactory = create_session,
        queue_size: int = _QUEUE_SIZE,
        retry_seconds: float = _RETRY_SECONDS,
        heartbeat_seconds: float = _HEARTBEAT_SECONDS,
        lease_timeout_seconds: float = _LEASE_TIMEOUT_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("Terminal outcome queue size must be positive")
        if retry_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("Terminal outcome retry intervals must be positive")
        if lease_timeout_seconds <= heartbeat_seconds:
            raise ValueError("Terminal outcome lease must exceed its heartbeat")
        self._session_factory = session_factory
        self._queue_size = queue_size
        self._retry_seconds = retry_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_timeout_ms = int(lease_timeout_seconds * 1000)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._queue: asyncio.Queue[_QueuedOutcome] | None = None
        self._task: asyncio.Task[None] | None = None
        self._idle: asyncio.Event | None = None
        self._wake: asyncio.Event | None = None
        self._run_id: str | None = None
        self._epoch = 0
        self._enabled = False
        self._accepting = False
        self._stopping = False
        self._loss_pending = False
        self._loss_day: date | None = None

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def loss_pending(self) -> bool:
        return self._loss_pending

    @property
    def loss_day(self) -> date | None:
        return self._loss_day

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, *, serving: bool = False) -> bool:
        """``serving`` means this process settled requests while not collecting."""
        if self.running:
            return True
        previous_loss_day = self._loss_day
        self._enabled = True
        self._accepting = False
        self._stopping = False
        self._loss_pending = False
        self._loss_day = None
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        self._idle = asyncio.Event()
        self._idle.set()
        self._wake = asyncio.Event()
        try:
            now = self._now_ms()
            await self._ensure_epoch(now)
            await self._recover_stale_runs(now)
            if previous_loss_day is not None:
                await self._rotate_epoch(previous_loss_day)
            await self._recover_unattended_coverage(now)
            if serving:
                await self._void_served_coverage(now)
            await self._create_run(now, "active")
        except Exception:
            self._loss_pending = True
            self._loss_day = previous_loss_day or _utc_day_from_ms(self._now_ms())
            self._clear_runtime()
            logger.critical("Terminal outcome writer could not start", exc_info=True)
            return False
        self._accepting = True
        self._task = self._new_task()
        return True

    async def stop(self, *, timeout: float = 5.0, close_coverage: bool = False) -> bool:
        closed_day = _utc_day_from_ms(self._now_ms()) - timedelta(days=1)
        if not self._enabled:
            if close_coverage:
                try:
                    await self._recover_unattended_coverage(self._now_ms())
                    await self._close_coverage(closed_day)
                except Exception:
                    logger.critical(
                        "Terminal outcome coverage closure failed", exc_info=True
                    )
                    return False
            return True
        self._stopping = True
        self._accepting = False
        if self._wake is not None:
            self._wake.set()
        clean = False
        drained = False
        task = self._task
        try:
            if self._queue is not None:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            if self._idle is not None:
                await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            drained = not self._loss_pending and task is not None and not task.done()
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            logger.critical("Terminal outcome queue shutdown failed", exc_info=True)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            if close_coverage:
                await self._close_coverage(closed_day)
            clean = drained and not self._loss_pending and await self._close_run()
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            logger.critical("Terminal outcome clean shutdown failed", exc_info=True)
        self._clear_runtime()
        return clean

    async def flush(self, *, timeout: float = 5.0) -> bool:
        if not self.running or self._queue is None or self._idle is None:
            return False
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return not self._loss_pending and await self._touch_run(drained=True)
        except (TimeoutError, asyncio.CancelledError):
            return False
        except Exception:
            logger.error("Terminal outcome flush failed", exc_info=True)
            return False

    def submit(self, outcome: _QueuedOutcome) -> bool:
        if not self._enabled:
            return False
        if not self._accepting or not self.running or self._queue is None:
            self.declare_loss(
                "terminal outcome writer unavailable", outcome.terminal_day
            )
            return False
        try:
            self._queue.put_nowait(outcome)
        except asyncio.QueueFull:
            self.declare_loss("terminal outcome queue full", outcome.terminal_day)
            return False
        if self._idle is not None:
            self._idle.clear()
        if self._wake is not None:
            self._wake.set()
        return True

    def declare_loss(self, reason: str, lost_day: date | None = None) -> None:
        if not self._enabled:
            return
        day = lost_day or _utc_day_from_ms(self._now_ms())
        first_loss = not self._loss_pending
        self._loss_pending = True
        if self._loss_day is None or day < self._loss_day:
            self._loss_day = day
        self._accepting = False
        if self._idle is not None:
            self._idle.clear()
        if self._wake is not None:
            self._wake.set()
        if first_loss:
            logger.critical(
                "Terminal outcome continuity lost",
                extra={"reason": reason, "epoch": self._epoch},
            )

    def _now_ms(self) -> int:
        now = self._clock()
        if not _valid_nonnegative_int(now):
            raise ValueError("Terminal outcome clock returned an invalid value")
        return now

    def _new_task(self) -> asyncio.Task[None]:
        task = asyncio.create_task(self._run(), name="terminal-outcome-writer")
        task.add_done_callback(self._writer_stopped)
        return task

    def _clear_runtime(self) -> None:
        self._task = None
        self._queue = None
        self._idle = None
        self._wake = None
        self._run_id = None
        self._enabled = False
        self._accepting = False
        self._stopping = False

    async def _ensure_epoch(self, now: int) -> None:
        while True:
            async with self._session_factory() as session:
                current = await _current_epoch(session)
                if current is not None:
                    self._epoch = current.epoch
                    return
                latest_result = await session.exec(
                    select(TerminalOutcomeEpoch).order_by(
                        col(TerminalOutcomeEpoch.epoch).desc()
                    )
                )
                latest = latest_result.first()
                next_epoch = 0 if latest is None else latest.epoch + 1
                session.add(
                    TerminalOutcomeEpoch(
                        epoch=next_epoch,
                        coverage_start_day=_utc_day_from_ms(now) + timedelta(days=1),
                        current_slot=1,
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
                self._epoch = next_epoch
                return

    async def _create_run(
        self,
        now: int,
        status: str,
        lost_day: date | None = None,
    ) -> None:
        run_id = uuid.uuid4().hex
        async with self._session_factory() as session:
            session.add(
                TerminalOutcomeWriterRun(
                    run_id=run_id,
                    status=status,
                    started_at_ms=now,
                    heartbeat_at_ms=now,
                    flushed_through_ms=now if status == "active" else None,
                    loss_day=lost_day,
                )
            )
            await session.commit()
        self._run_id = run_id

    async def _update_run(self, expected: tuple[str, ...], **values: object) -> bool:
        if self._run_id is None:
            return False
        async with self._session_factory() as session:
            result = await session.exec(  # type: ignore[call-overload]
                update(TerminalOutcomeWriterRun)
                .where(col(TerminalOutcomeWriterRun.run_id) == self._run_id)
                .where(col(TerminalOutcomeWriterRun.status).in_(expected))
                .values(**values)
            )
            await session.commit()
        return bool(result.rowcount == 1)

    async def _close_coverage(self, closed_day: date) -> None:
        async with self._session_factory() as session:
            await session.exec(  # type: ignore[call-overload]
                update(TerminalOutcomeEpoch)
                .where(col(TerminalOutcomeEpoch.current_slot) == 1)
                .values(coverage_end_day=closed_day, current_slot=None)
            )
            await session.commit()

    async def _close_run(self) -> bool:
        now = self._now_ms()
        return await self._update_run(
            ("active",),
            status="clean",
            heartbeat_at_ms=now,
            closed_at_ms=now,
        )

    async def _touch_run(self, *, drained: bool = False) -> bool:
        now = self._now_ms()
        values = {"heartbeat_at_ms": now}
        if drained and not self._loss_pending:
            values["flushed_through_ms"] = now
        touched = await self._update_run(("active", "degraded"), **values)
        if not touched:
            self.declare_loss("terminal outcome writer lease was lost")
        return touched

    async def _mark_degraded(self, lost_day: date) -> None:
        updated = await self._update_run(
            ("active", "degraded"),
            status="degraded",
            loss_day=lost_day,
        )
        if not updated:
            await self._create_run(self._now_ms(), "degraded", lost_day)

    async def _activate_run(self) -> None:
        now = self._now_ms()
        updated = await self._update_run(
            ("degraded",),
            status="active",
            heartbeat_at_ms=now,
            flushed_through_ms=now,
            loss_day=None,
        )
        if not updated:
            await self._create_run(now, "active")

    async def _claim_stale_runs(self, now: int) -> None:
        cutoff = now - self._lease_timeout_ms
        async with self._session_factory() as session:
            statement = (
                select(TerminalOutcomeWriterRun)
                .where(col(TerminalOutcomeWriterRun.status).in_(("active", "degraded")))
                .where(col(TerminalOutcomeWriterRun.heartbeat_at_ms) < cutoff)
            )
            if self._run_id is not None:
                statement = statement.where(
                    col(TerminalOutcomeWriterRun.run_id) != self._run_id
                )
            stale_runs = (await session.exec(statement)).all()
            claimed = 0
            for stale in stale_runs:
                checkpoint_day = _utc_day_from_ms(
                    stale.flushed_through_ms or stale.started_at_ms
                )
                lost_day = min(checkpoint_day, stale.loss_day or checkpoint_day)
                result = await session.exec(  # type: ignore[call-overload]
                    update(TerminalOutcomeWriterRun)
                    .where(col(TerminalOutcomeWriterRun.run_id) == stale.run_id)
                    .where(col(TerminalOutcomeWriterRun.status) == stale.status)
                    .where(
                        col(TerminalOutcomeWriterRun.heartbeat_at_ms)
                        == stale.heartbeat_at_ms
                    )
                    .values(
                        status="lost",
                        closed_at_ms=now,
                        loss_day=lost_day,
                    )
                )
                claimed += int(result.rowcount == 1)
            await session.commit()
        if claimed:
            logger.critical(
                "Stale terminal outcome writer lease detected",
                extra={"stale_runs": claimed},
            )

    async def _stage_rotation(
        self,
        session: AsyncSession,
        current: TerminalOutcomeEpoch,
        lost_day: date,
    ) -> int | None:
        transition = await session.exec(  # type: ignore[call-overload]
            update(TerminalOutcomeEpoch)
            .where(col(TerminalOutcomeEpoch.epoch) == current.epoch)
            .where(col(TerminalOutcomeEpoch.current_slot) == 1)
            .values(
                coverage_end_day=lost_day - timedelta(days=1),
                current_slot=None,
            )
        )
        if transition.rowcount != 1:
            return None
        await session.exec(  # type: ignore[call-overload]
            update(TerminalOutcomeEpoch)
            .where(col(TerminalOutcomeEpoch.current_slot).is_(None))
            .where(col(TerminalOutcomeEpoch.coverage_end_day) >= lost_day)
            .values(
                coverage_end_day=lost_day - timedelta(days=1),
            )
        )
        next_epoch = current.epoch + 1
        recovery_day = _utc_day_from_ms(self._now_ms())
        session.add(
            TerminalOutcomeEpoch(
                epoch=next_epoch,
                coverage_start_day=max(lost_day, recovery_day) + timedelta(days=1),
                current_slot=1,
            )
        )
        return next_epoch

    async def _rotate_epoch(self, lost_day: date) -> None:
        while True:
            now = self._now_ms()
            async with self._session_factory() as session:
                current = await _current_epoch(session)
                if current is None:
                    await self._ensure_epoch(now)
                    continue
                next_epoch = await self._stage_rotation(session, current, lost_day)
                if next_epoch is None:
                    await session.rollback()
                    continue
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
                self._epoch = next_epoch
                return

    async def _recover_pending_runs(self) -> None:
        while True:
            now = self._now_ms()
            async with self._session_factory() as session:
                pending = (
                    await session.exec(
                        select(TerminalOutcomeWriterRun).where(
                            col(TerminalOutcomeWriterRun.status) == "lost"
                        )
                    )
                ).all()
                loss_days = [
                    run.loss_day for run in pending if run.loss_day is not None
                ]
                if not loss_days:
                    return
                pending_ids = [run.run_id for run in pending]
                current = await _current_epoch(session)
                if current is None:
                    await self._ensure_epoch(now)
                    continue
                claimed = await session.exec(  # type: ignore[call-overload]
                    update(TerminalOutcomeWriterRun)
                    .where(col(TerminalOutcomeWriterRun.status) == "lost")
                    .where(col(TerminalOutcomeWriterRun.run_id).in_(pending_ids))
                    .values(status="recovered")
                )
                if claimed.rowcount == 0:
                    await session.rollback()
                    continue
                next_epoch = await self._stage_rotation(
                    session, current, min(loss_days)
                )
                if next_epoch is None:
                    await session.rollback()
                    continue
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
                self._epoch = next_epoch
                return

    async def _recover_stale_runs(self, now: int) -> None:
        await self._claim_stale_runs(now)
        await self._recover_pending_runs()

    async def _recover_unattended_coverage(self, now: int) -> None:
        while True:
            async with self._session_factory() as session:
                current = await _current_epoch(session)
                if current is None or current.coverage_start_day > _utc_day_from_ms(
                    now
                ):
                    return
                live = (
                    await session.exec(
                        select(TerminalOutcomeWriterRun.run_id)
                        .where(
                            col(TerminalOutcomeWriterRun.status).in_(
                                ("active", "degraded", "lost")
                            )
                        )
                        .limit(1)
                    )
                ).first()
                if live is not None:
                    return
                last_clean_close = (
                    await session.exec(
                        select(TerminalOutcomeWriterRun.closed_at_ms)
                        .where(col(TerminalOutcomeWriterRun.status) == "clean")
                        .where(col(TerminalOutcomeWriterRun.closed_at_ms).is_not(None))
                        .order_by(col(TerminalOutcomeWriterRun.closed_at_ms).desc())
                        .limit(1)
                    )
                ).first()
                # A clean stop proves its drained records, not later process uptime.
                lost_day = current.coverage_start_day
                if last_clean_close is not None:
                    lost_day = max(lost_day, _utc_day_from_ms(last_clean_close))
                next_epoch = await self._stage_rotation(session, current, lost_day)
                if next_epoch is None:
                    await session.rollback()
                    continue
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
                self._epoch = next_epoch
                return

    async def _void_served_coverage(self, now: int) -> None:
        async with self._session_factory() as session:
            current = await _current_epoch(session)
        # Coverage that already began cannot include what this worker missed.
        if current is not None and current.coverage_start_day <= _utc_day_from_ms(now):
            await self._rotate_epoch(current.coverage_start_day)

    async def _reconcile(self, queued: _QueuedOutcome) -> _PersistResult:
        try:
            async with self._session_factory() as session:
                existing = await session.get(TerminalOutcome, queued.outcome_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _PersistResult.RETRY
        if existing is None:
            return _PersistResult.RETRY
        return (
            _PersistResult.STORED
            if _same_outcome(existing, queued)
            else _PersistResult.CONFLICT
        )

    async def _persist_once(self, queued: _QueuedOutcome) -> _PersistResult:
        try:
            async with self._session_factory() as session:
                session.add(TerminalOutcome(**asdict(queued)))
                await session.commit()
                return _PersistResult.STORED
        except asyncio.CancelledError:
            raise
        except Exception:
            return await self._reconcile(queued)

    async def _persist(self, queued: _QueuedOutcome) -> _PersistResult | None:
        logged = False
        while not self._loss_pending:
            result = await self._persist_once(queued)
            if result is not _PersistResult.RETRY:
                return result
            if not logged:
                logger.error(
                    "Terminal outcome persistence will be retried",
                    extra={"outcome_id": queued.outcome_id},
                )
                logged = True
            try:
                await self._touch_run()
                await self._recover_stale_runs(self._now_ms())
            except Exception:
                pass
            await asyncio.sleep(self._retry_seconds)
        self.declare_loss(
            "in-flight terminal outcome abandoned after continuity loss",
            queued.terminal_day,
        )
        return None

    def _discard_queue(self) -> None:
        if self._queue is None:
            return
        while True:
            try:
                queued = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                self.declare_loss(
                    "queued terminal outcome discarded after continuity loss",
                    queued.terminal_day,
                )
                self._queue.task_done()

    async def _recover_loss(self) -> None:
        self._discard_queue()
        rotated_for: date | None = None
        while self._loss_pending:
            lost_day = self._loss_day or _utc_day_from_ms(self._now_ms())
            try:
                await self._recover_stale_runs(self._now_ms())
                await self._mark_degraded(lost_day)
                if rotated_for is None or lost_day < rotated_for:
                    await self._rotate_epoch(lost_day)
                    rotated_for = lost_day
                await self._activate_run()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.critical(
                    "Terminal outcome continuity recovery failed", exc_info=True
                )
                await asyncio.sleep(self._retry_seconds)
                continue
            if self._loss_day is not None and self._loss_day < lost_day:
                continue
            self._loss_pending = False
            self._loss_day = None
            if not self._stopping:
                self._accepting = True

    async def _heartbeat(self) -> None:
        try:
            await self._touch_run(
                drained=self._queue is not None and self._queue.empty()
            )
            await self._recover_stale_runs(self._now_ms())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Terminal outcome heartbeat failed", exc_info=True)

    async def _run(self) -> None:
        if self._queue is None or self._wake is None:
            return
        next_heartbeat = time.monotonic() + self._heartbeat_seconds
        while True:
            try:
                if self._loss_pending:
                    await self._recover_loss()
                self._wake.clear()
                try:
                    queued = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    if self._idle is not None:
                        self._idle.set()
                    try:
                        await asyncio.wait_for(
                            self._wake.wait(), timeout=self._heartbeat_seconds
                        )
                    except TimeoutError:
                        await self._heartbeat()
                        next_heartbeat = time.monotonic() + self._heartbeat_seconds
                    continue
                try:
                    result = await self._persist(queued)
                    if result is _PersistResult.CONFLICT:
                        self.declare_loss(
                            "conflicting terminal outcome idempotency key",
                            queued.terminal_day,
                        )
                finally:
                    self._queue.task_done()
                if time.monotonic() >= next_heartbeat:
                    await self._heartbeat()
                    next_heartbeat = time.monotonic() + self._heartbeat_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.critical("Terminal outcome writer failed", exc_info=True)
                self.declare_loss("terminal outcome writer failed")
                await asyncio.sleep(self._retry_seconds)

    def _writer_stopped(self, task: asyncio.Task[None]) -> None:
        if self._stopping or task.cancelled() or not self._enabled:
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        self.declare_loss(
            "terminal outcome writer stopped"
            if error is None
            else f"terminal outcome writer stopped: {type(error).__name__}"
        )
        self._task = self._new_task()


terminal_outcome_writer = TerminalOutcomeWriter()


def _valid_nonnegative_int(value: object, maximum: int | None = None) -> bool:
    return type(value) is int and value >= 0 and (maximum is None or value <= maximum)


def record_terminal_outcome(
    context: TerminalOutcomeContext,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
    revenue_msats: int,
    terminal_at_ms: int | None = None,
    usage: object = None,
) -> None:
    """Submit a settled outcome without awaiting storage or raising.

    ``usage`` is the raw upstream usage. It replaces the token counts and is
    parsed here, so a malformed value can only mark a gap.
    """
    try:
        if usage is not None:
            from ..payment.usage import NormalizedUsage, normalize_usage

            counted = normalize_usage(usage) or NormalizedUsage()
            input_tokens = counted.input_tokens
            output_tokens = counted.output_tokens
            cache_read_input_tokens = counted.cache_read_tokens
            cache_creation_input_tokens = counted.cache_write_tokens
        observed = (
            context.input_observed,
            context.output_observed,
            context.cache_read_observed,
            context.cache_creation_observed,
        )
        sources = {
            name + "_source": getattr(context, name + "_source")
            or ("reported" if getattr(context, name + "_observed") else "missing")
            for name in ("input", "output", "cache_read", "cache_creation")
        }
        tokens = (
            input_tokens,
            output_tokens,
            cache_read_input_tokens,
            cache_creation_input_tokens,
        )
        timestamp = (
            terminal_at_ms if terminal_at_ms is not None else int(time.time() * 1000)
        )
        terminal_day = _utc_day_from_ms(timestamp)
        if (
            not context.outcome_id
            or any(
                source not in {"reported", "estimated", "missing"}
                for source in sources.values()
            )
            or any(value is not None and type(value) is not bool for value in observed)
            or any(not _valid_nonnegative_int(value, _MAX_TOKENS) for value in tokens)
            or not _valid_nonnegative_int(revenue_msats, _MAX_REVENUE_MSATS)
            or not _valid_nonnegative_int(timestamp)
        ):
            terminal_outcome_writer.declare_loss(
                "invalid settled terminal outcome", terminal_day
            )
            return
        terminal_outcome_writer.submit(
            _QueuedOutcome(
                outcome_id=context.outcome_id,
                terminal_at_ms=timestamp,
                terminal_day=terminal_day,
                model_identifier=context.model_identifier,
                served_model_identifier=context.served_model_identifier,
                pricing_source=context.pricing_source,
                **sources,
                input_observed=context.input_observed,
                output_observed=context.output_observed,
                cache_read_observed=context.cache_read_observed,
                cache_creation_observed=context.cache_creation_observed,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                revenue_msats=revenue_msats,
            )
        )
    except BaseException:
        try:
            terminal_outcome_writer.declare_loss("terminal outcome submission raised")
            logger.critical("Terminal outcome submission failed", exc_info=True)
        except BaseException:
            pass


def mark_terminal_outcome_loss(reason: str) -> None:
    try:
        terminal_outcome_writer.declare_loss(reason)
    except BaseException:
        pass


def cashu_retained_msats(amount: int, unit: str, refund_amount: int = 0) -> int | None:
    try:
        if not _valid_nonnegative_int(amount) or not _valid_nonnegative_int(
            refund_amount
        ):
            raise ValueError("Cashu amounts must be nonnegative integers")
        if refund_amount > amount:
            raise ValueError("Cashu refund exceeds redeemed amount")
        if unit == "msat":
            multiplier = 1
        elif unit == "sat":
            multiplier = 1000
        else:
            raise ValueError(f"Unsupported Cashu unit: {unit}")
        return (amount - refund_amount) * multiplier
    except Exception:
        mark_terminal_outcome_loss("invalid Cashu retained value")
        return None


async def start_terminal_outcome_writer(*, serving: bool = False) -> bool:
    return await terminal_outcome_writer.start(serving=serving)


async def stop_terminal_outcome_writer(
    *, timeout: float = 5.0, close_coverage: bool = False
) -> bool:
    return await terminal_outcome_writer.stop(
        timeout=timeout, close_coverage=close_coverage
    )
