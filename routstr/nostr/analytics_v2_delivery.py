from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, AsyncContextManager, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import websockets
from aiohttp.abc import AbstractResolver
from sqlalchemy import and_, func
from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import col, or_, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from ..core.db import (
    AnalyticsV2DeliveryState,
    AnalyticsV2Outbox,
    AnalyticsV2RelayReceipt,
    TerminalOutcome,
    TerminalOutcomeEpoch,
    TerminalOutcomeWriterRun,
)
from ..core.logging import get_logger
from .analytics_v2 import (
    ANALYTICS_KIND,
    DEFAULT_MAX_FRAME_BYTES,
    EncodedAnalyticsEvent,
    LedgerOutcome,
    PriorVersion,
    WeeklyAggregate,
    aggregate_ledger_week,
    build_analytics_address,
    daily_models_changed,
    encode_week_event,
    prior_version_from_frame,
)

logger = get_logger(__name__)

ANALYTICS_RELAY_QUORUM = 2
PUBLIC_HISTORY_DAYS = 365
NIP11_MAX_DOCUMENT_BYTES = 65_536

SessionFactory = Callable[[], AsyncContextManager[AsyncSession]]
IdentityClaim = Literal["initialized", "matched", "mismatch"]


class AnalyticsV2DeliveryError(RuntimeError):
    pass


class SharingDisabledError(AnalyticsV2DeliveryError):
    pass


class IdentityMismatchError(AnalyticsV2DeliveryError):
    pass


class OutboxConflictError(AnalyticsV2DeliveryError):
    pass


def _require_lower_hex(value: object, length: int, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != length
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AnalyticsV2DeliveryError(
            f"{name} must be {length} lowercase hex characters"
        )


def _normalize_public_wss_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise AnalyticsV2DeliveryError("Relay URL is empty or not normalized")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise AnalyticsV2DeliveryError("Relay URL has an invalid port") from error
    if parsed.scheme.lower() != "wss" or not parsed.hostname:
        raise AnalyticsV2DeliveryError("Relay URL must use wss")
    if parsed.username is not None or parsed.password is not None:
        raise AnalyticsV2DeliveryError("Relay URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise AnalyticsV2DeliveryError("Relay URL must not contain query credentials")

    host = parsed.hostname.rstrip(".").lower()
    if not host:
        raise AnalyticsV2DeliveryError("Relay URL hostname is empty")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            "." not in host
            or host.endswith(
                (
                    ".local",
                    ".localhost",
                    ".internal",
                    ".home",
                    ".lan",
                    ".test",
                    ".invalid",
                    ".example",
                )
            )
            or re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
                host,
            )
            is None
        ):
            raise AnalyticsV2DeliveryError("Relay hostname is not public")
        rendered_host = host
    else:
        if not address.is_global:
            raise AnalyticsV2DeliveryError("Relay IP address is not public")
        rendered_host = f"[{host}]" if address.version == 6 else host

    if port is not None and not 1 <= port <= 65535:
        raise AnalyticsV2DeliveryError("Relay URL port is invalid")
    netloc = rendered_host if port in {None, 443} else f"{rendered_host}:{port}"
    path = "" if parsed.path in {"", "/"} else parsed.path
    return urlunsplit(("wss", netloc, path, "", ""))


@dataclass(frozen=True)
class RelayTarget:
    url: str


RelayLimitReader = Callable[[RelayTarget, float], Awaitable[int | None]]


@dataclass(frozen=True)
class ResolvedRelayEndpoint:
    address: str
    port: int
    server_hostname: str


@dataclass(frozen=True)
class DeliveryStateSnapshot:
    sharing_enabled: bool
    generation: int
    identity_pubkey: str | None
    provider_d: str | None
    updated_at_ms: int
    active_epoch_floor: int | None = None


@dataclass(frozen=True)
class ActivationResult:
    state: DeliveryStateSnapshot
    transitioned: bool


@dataclass(frozen=True)
class EnqueueResult:
    event_id: str
    inserted: bool
    created_at: int


@dataclass(frozen=True)
class RelaySendResult:
    accepted: bool
    read_back: bool


@dataclass(frozen=True)
class DeliveryPassResult:
    attempted_events: int
    delivered_events: int


@dataclass(frozen=True)
class _PendingFrame:
    event_id: str
    frame: bytes
    delivery_generation: int


class RelaySender(Protocol):
    async def __call__(
        self,
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult: ...


class _PinnedRelayResolver(AbstractResolver):
    def __init__(self, endpoint: ResolvedRelayEndpoint) -> None:
        self._endpoint = endpoint

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[Any]:
        if host != self._endpoint.server_hostname:
            raise OSError("Relay information lookup changed hostname")
        address = ipaddress.ip_address(self._endpoint.address)
        return [
            {
                "hostname": host,
                "host": address.compressed,
                "port": self._endpoint.port,
                "family": (socket.AF_INET6 if address.version == 6 else socket.AF_INET),
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        return None


async def fetch_relay_max_message_length(
    target: RelayTarget, timeout_seconds: float
) -> int | None:
    """Read an advertised NIP-11 frame limit through the pinned public endpoint."""
    try:
        endpoint = await resolve_public_relay_endpoint(target.url)
        parsed = urlsplit(target.url)
        relay_information_url = urlunsplit(
            ("https", parsed.netloc, parsed.path or "/", "", "")
        )
        connector = aiohttp.TCPConnector(
            resolver=_PinnedRelayResolver(endpoint),
            use_dns_cache=False,
            force_close=True,
        )
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session:
            async with session.get(
                relay_information_url,
                headers={"Accept": "application/nostr+json"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return None
                if (
                    response.content_length is not None
                    and response.content_length > NIP11_MAX_DOCUMENT_BYTES
                ):
                    return None
                body = await response.content.read(NIP11_MAX_DOCUMENT_BYTES + 1)
                if len(body) > NIP11_MAX_DOCUMENT_BYTES:
                    return None
                payload = json.loads(body)
        if not isinstance(payload, dict):
            return None
        limitation = payload.get("limitation")
        if not isinstance(limitation, dict):
            return None
        maximum = limitation.get("max_message_length")
        if type(maximum) is int and maximum > 0:
            return maximum
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    return None


async def get_analytics_v2_delivery_state(
    session_factory: SessionFactory,
    *,
    at_ms: int | None = None,
) -> DeliveryStateSnapshot:
    timestamp = _clock_ms() if at_ms is None else at_ms
    _require_nonnegative_int(timestamp, "at_ms")
    async with session_factory() as session:
        state = await _get_or_create_delivery_state(session, timestamp)
        snapshot = _snapshot(state)
        await session.commit()
        return snapshot


async def claim_analytics_v2_identity(
    session_factory: SessionFactory,
    *,
    pubkey: str,
    provider_d: str,
    at_ms: int | None = None,
) -> IdentityClaim:
    _validate_identity(pubkey, provider_d)
    timestamp = _clock_ms() if at_ms is None else at_ms
    _require_nonnegative_int(timestamp, "at_ms")
    while True:
        async with session_factory() as session:
            try:
                state = await _get_or_create_delivery_state(session, timestamp)
                claimed = await session.exec(  # type: ignore[call-overload]
                    update(AnalyticsV2DeliveryState)
                    .where(col(AnalyticsV2DeliveryState.id) == 1)
                    .where(col(AnalyticsV2DeliveryState.identity_pubkey).is_(None))
                    .where(col(AnalyticsV2DeliveryState.provider_d).is_(None))
                    .values(
                        identity_pubkey=pubkey,
                        provider_d=provider_d,
                        updated_at_ms=timestamp,
                    )
                )
                if claimed.rowcount == 1:
                    await session.commit()
                    return "initialized"
                await session.refresh(state)
                result: IdentityClaim = (
                    "matched"
                    if state.identity_pubkey == pubkey
                    and state.provider_d == provider_d
                    else "mismatch"
                )
                await session.commit()
                return result
            except (IntegrityError, OperationalError):
                await session.rollback()
                await asyncio.sleep(0)


async def transition_analytics_v2_sharing(
    session_factory: SessionFactory,
    *,
    enabled: bool,
    at_ms: int | None = None,
) -> DeliveryStateSnapshot:
    if not isinstance(enabled, bool):
        raise AnalyticsV2DeliveryError("enabled must be a boolean")
    timestamp = _clock_ms() if at_ms is None else at_ms
    _require_nonnegative_int(timestamp, "at_ms")
    while True:
        async with session_factory() as session:
            try:
                state = await _get_or_create_delivery_state(session, timestamp)
                if enabled and not state.sharing_enabled:
                    raise AnalyticsV2DeliveryError(
                        "Use activate_analytics_v2_sharing to enable publication"
                    )
                if state.sharing_enabled != enabled:
                    # Private collection keeps its coverage; activation sets the
                    # next public floor.
                    state.sharing_enabled = enabled
                    state.generation += 1
                    state.active_epoch_floor = None
                    state.updated_at_ms = timestamp
                    if not enabled:
                        await session.exec(  # type: ignore[call-overload]
                            update(AnalyticsV2Outbox)
                            .where(col(AnalyticsV2Outbox.status) == "pending")
                            .values(status="cancelled")
                        )
                snapshot = _snapshot(state)
                await session.commit()
                return snapshot
            except OperationalError:
                await session.rollback()
                await asyncio.sleep(0)


async def fence_analytics_v2_opt_out(session: AsyncSession) -> None:
    """Disable sharing inside the caller's transaction.

    Committed together with a saved opt-out, the new generation refuses any
    activation that read its flags before the save.
    """
    await session.exec(  # type: ignore[call-overload]
        update(AnalyticsV2DeliveryState)
        .where(col(AnalyticsV2DeliveryState.id) == 1)
        .values(
            sharing_enabled=False,
            generation=AnalyticsV2DeliveryState.generation + 1,
            active_epoch_floor=None,
            updated_at_ms=_clock_ms(),
        )
    )
    await session.exec(  # type: ignore[call-overload]
        update(AnalyticsV2Outbox)
        .where(col(AnalyticsV2Outbox.status) == "pending")
        .values(status="cancelled")
    )


async def activate_analytics_v2_sharing(
    session_factory: SessionFactory,
    *,
    coverage_day: date,
    at_ms: int | None = None,
    expected_generation: int | None = None,
) -> ActivationResult:
    """Rotate continuity and expose a false-to-true transition atomically.

    ``expected_generation`` refuses a caller whose flags predate an opt-out.
    """
    if type(coverage_day) is not date:
        raise AnalyticsV2DeliveryError("coverage_day must be a date")
    timestamp = _clock_ms() if at_ms is None else at_ms
    _require_nonnegative_int(timestamp, "at_ms")

    while True:
        async with session_factory() as session:
            state_result = await session.exec(
                select(AnalyticsV2DeliveryState)
                .where(col(AnalyticsV2DeliveryState.id) == 1)
                .with_for_update()
            )
            state = state_result.first()
            if state is None:
                state = AnalyticsV2DeliveryState(id=1, updated_at_ms=timestamp)
                session.add(state)
                try:
                    await session.flush()
                except (IntegrityError, OperationalError):
                    await session.rollback()
                    await asyncio.sleep(0)
                    continue
            if state.sharing_enabled:
                snapshot = _snapshot(state)
                await session.commit()
                return ActivationResult(snapshot, False)
            if expected_generation not in (None, state.generation):
                raise SharingDisabledError("Analytics v2 sharing changed meanwhile")
            if state.identity_pubkey is None or state.provider_d is None:
                raise IdentityMismatchError(
                    "Analytics v2 identity must be claimed before activation"
                )

            epoch_result = await session.exec(
                select(TerminalOutcomeEpoch)
                .where(col(TerminalOutcomeEpoch.current_slot) == 1)
                .with_for_update()
            )
            current = epoch_result.first()
            if current is None:
                latest_result = await session.exec(
                    select(TerminalOutcomeEpoch).order_by(
                        col(TerminalOutcomeEpoch.epoch).desc()
                    )
                )
                latest = latest_result.first()
                next_epoch = 0 if latest is None else latest.epoch + 1
                current = TerminalOutcomeEpoch(
                    epoch=next_epoch,
                    coverage_start_day=coverage_day + timedelta(days=1),
                    current_slot=1,
                )
                session.add(current)
                try:
                    await session.flush()
                except (IntegrityError, OperationalError):
                    await session.rollback()
                    await asyncio.sleep(0)
                    continue
            elif current.coverage_start_day != coverage_day + timedelta(days=1):
                try:
                    await _close_current_epoch(
                        session,
                        coverage_end_day=coverage_day - timedelta(days=1),
                    )
                except OperationalError:
                    await session.rollback()
                    await asyncio.sleep(0)
                    continue
                latest_result = await session.exec(
                    select(TerminalOutcomeEpoch).order_by(
                        col(TerminalOutcomeEpoch.epoch).desc()
                    )
                )
                latest = latest_result.first()
                next_epoch = 0 if latest is None else latest.epoch + 1
                current = TerminalOutcomeEpoch(
                    epoch=next_epoch,
                    coverage_start_day=coverage_day + timedelta(days=1),
                    current_slot=1,
                )
                session.add(current)
                try:
                    await session.flush()
                except (IntegrityError, OperationalError):
                    await session.rollback()
                    await asyncio.sleep(0)
                    continue
            await session.exec(  # type: ignore[call-overload]
                update(AnalyticsV2Outbox)
                .where(col(AnalyticsV2Outbox.status) == "pending")
                .values(status="cancelled")
            )
            # Compare-and-set: SQLite ignores FOR UPDATE, so the row read above
            # may already be stale.
            claimed = await session.exec(  # type: ignore[call-overload]
                update(AnalyticsV2DeliveryState)
                .where(col(AnalyticsV2DeliveryState.id) == 1)
                .where(col(AnalyticsV2DeliveryState.sharing_enabled).is_(False))
                .where(col(AnalyticsV2DeliveryState.generation) == state.generation)
                .values(
                    sharing_enabled=True,
                    generation=state.generation + 1,
                    active_epoch_floor=current.epoch,
                    updated_at_ms=timestamp,
                )
            )
            if claimed.rowcount != 1:
                await session.rollback()
                await asyncio.sleep(0)
                continue
            await session.refresh(state)
            snapshot = _snapshot(state)
            try:
                await session.commit()
            except (IntegrityError, OperationalError):
                await session.rollback()
                await asyncio.sleep(0)
                continue
            return ActivationResult(snapshot, True)


async def rotate_analytics_v2_identity(
    session_factory: SessionFactory,
    *,
    pubkey: str,
    provider_d: str,
    at_ms: int | None = None,
) -> DeliveryStateSnapshot:
    """Bind a changed identity behind a disabled delivery generation."""
    _validate_identity(pubkey, provider_d)
    timestamp = _clock_ms() if at_ms is None else at_ms
    _require_nonnegative_int(timestamp, "at_ms")
    async with session_factory() as session:
        state = await _get_or_create_delivery_state(session, timestamp)
        if state.identity_pubkey != pubkey or state.provider_d != provider_d:
            state.sharing_enabled = False
            state.generation += 1
            state.active_epoch_floor = None
            state.identity_pubkey = pubkey
            state.provider_d = provider_d
            state.updated_at_ms = timestamp
            await session.exec(  # type: ignore[call-overload]
                update(AnalyticsV2Outbox)
                .where(col(AnalyticsV2Outbox.status) == "pending")
                .values(status="cancelled")
            )
        snapshot = _snapshot(state)
        await session.commit()
        return snapshot


async def enqueue_signed_event(
    session_factory: SessionFactory,
    event: EncodedAnalyticsEvent,
    *,
    stored_at_ms: int | None = None,
) -> EnqueueResult:
    """Commit exact signed bytes and coalesce older pending open versions."""
    timestamp = _clock_ms() if stored_at_ms is None else stored_at_ms
    _require_nonnegative_int(timestamp, "stored_at_ms")
    parsed = prior_version_from_frame(event.frame)
    _validate_encoded_metadata(event, parsed)
    semantic_slot = _semantic_slot(event)

    async with session_factory() as session:
        state = await _get_or_create_delivery_state(session, timestamp)
        if not state.sharing_enabled:
            raise SharingDisabledError("Analytics v2 sharing is disabled")
        if state.active_epoch_floor is None or event.epoch < state.active_epoch_floor:
            raise SharingDisabledError(
                "Analytics v2 event is outside the active epoch lane"
            )
        if (
            state.identity_pubkey != event.pubkey
            or state.provider_d is None
            or build_analytics_address(
                event.pubkey, state.provider_d, event.week, event.epoch
            ).d_tag
            != event.d_tag
        ):
            raise IdentityMismatchError(
                "Analytics v2 identity does not match the event"
            )
        generation = state.generation

        existing = await session.get(AnalyticsV2Outbox, event.event_id)
        if existing is not None:
            _require_exact_outbox(existing, event, generation, semantic_slot)
            await session.commit()
            return EnqueueResult(event.event_id, False, existing.created_at)

        winner_result = await session.exec(
            select(AnalyticsV2Outbox)
            .where(col(AnalyticsV2Outbox.pubkey) == event.pubkey)
            .where(col(AnalyticsV2Outbox.d_tag) == event.d_tag)
            .where(col(AnalyticsV2Outbox.semantic_slot) == semantic_slot)
        )
        winner = winner_result.first()
        if winner is not None:
            try:
                _require_semantic_winner(winner, event, generation, semantic_slot)
            except OutboxConflictError:
                if (
                    winner.status != "pending"
                    or winner.first_send_attempt_at_ms is not None
                ):
                    raise
                winner.status = "superseded"
                winner.semantic_slot = f"superseded:{winner.event_id}"
                await session.flush()
            else:
                await session.commit()
                return EnqueueResult(winner.event_id, False, winner.created_at)

        await session.exec(  # type: ignore[call-overload]
            update(AnalyticsV2Outbox)
            .where(col(AnalyticsV2Outbox.pubkey) == event.pubkey)
            .where(col(AnalyticsV2Outbox.d_tag) == event.d_tag)
            .where(col(AnalyticsV2Outbox.epoch) == event.epoch)
            .where(col(AnalyticsV2Outbox.delivery_generation) == state.generation)
            .where(col(AnalyticsV2Outbox.status) == "pending")
            .where(col(AnalyticsV2Outbox.semantic_slot) != semantic_slot)
            .where(
                or_(
                    col(AnalyticsV2Outbox.finalized).is_(False),
                    col(AnalyticsV2Outbox.first_send_attempt_at_ms).is_(None),
                    # A correction replaces the exact report it names.
                    col(AnalyticsV2Outbox.event_id)
                    == semantic_slot.removeprefix("correction:"),
                )
            )
            .values(status="superseded")
        )
        row = AnalyticsV2Outbox(
            event_id=event.event_id,
            pubkey=event.pubkey,
            d_tag=event.d_tag,
            kind=ANALYTICS_KIND,
            week=event.week,
            epoch=event.epoch,
            through_day=event.through,
            semantic_slot=semantic_slot,
            delivery_generation=generation,
            frame=event.frame,
            finalized=event.complete,
            corrected=event.corrected,
            status="pending",
            created_at=event.created_at,
            stored_at_ms=timestamp,
            next_attempt_at_ms=timestamp,
            attempt_count=0,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.get(AnalyticsV2Outbox, event.event_id)
            if existing is not None:
                _require_exact_outbox(existing, event, generation, semantic_slot)
                return EnqueueResult(event.event_id, False, existing.created_at)
            winner_result = await session.exec(
                select(AnalyticsV2Outbox)
                .where(col(AnalyticsV2Outbox.pubkey) == event.pubkey)
                .where(col(AnalyticsV2Outbox.d_tag) == event.d_tag)
                .where(col(AnalyticsV2Outbox.semantic_slot) == semantic_slot)
            )
            winner = winner_result.first()
            if winner is None:
                raise
            _require_semantic_winner(winner, event, generation, semantic_slot)
            return EnqueueResult(winner.event_id, False, winner.created_at)
    return EnqueueResult(event.event_id, True, event.created_at)


class AnalyticsV2Producer:
    """Turn the current continuity epoch into durable weekly signed events."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        private_key_hex: str,
        public_key_hex: str,
        provider_d: str,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        _validate_identity(public_key_hex, provider_d)
        self._session_factory = session_factory
        self._private_key_hex = private_key_hex
        self._public_key_hex = public_key_hex
        self._provider_d = provider_d
        self._max_frame_bytes = max_frame_bytes
        self._lock = asyncio.Lock()

    @property
    def session_factory(self) -> SessionFactory:
        return self._session_factory

    async def produce_once(
        self, *, now: datetime | None = None, max_frame_bytes: int | None = None
    ) -> int:
        frame_limit = min(
            self._max_frame_bytes, max_frame_bytes or self._max_frame_bytes
        )
        instant = datetime.now(UTC) if now is None else now
        if instant.tzinfo is None:
            raise AnalyticsV2DeliveryError("Producer time must be timezone-aware")
        instant = instant.astimezone(UTC)
        timestamp_ms = int(instant.timestamp() * 1000)
        async with self._lock:
            state = await get_analytics_v2_delivery_state(
                self._session_factory, at_ms=timestamp_ms
            )
            if not state.sharing_enabled:
                return 0
            if (
                state.identity_pubkey != self._public_key_hex
                or state.provider_d != self._provider_d
            ):
                raise IdentityMismatchError("Analytics v2 producer identity changed")

            if state.active_epoch_floor is None:
                return 0
            completed_day_limit = await self._completed_day_limit(instant.date())
            epochs, outcomes = await self._load_eligible_epochs(
                state.active_epoch_floor, completed_day_limit
            )
            if not epochs:
                return 0
            latest, created_at_floors, rows_by_id = await self._load_version_state(
                state.active_epoch_floor, state.generation
            )
            produced = 0
            yesterday = completed_day_limit - timedelta(days=1)
            # Each week aggregates only its own rows instead of rescanning all.
            by_week: dict[date, list[LedgerOutcome]] = {}
            for outcome in outcomes:
                day = outcome.terminal_day
                by_week.setdefault(day - timedelta(days=day.weekday()), []).append(
                    outcome
                )
            for epoch in epochs:
                coverage_end = min(epoch.coverage_end_day or yesterday, yesterday)
                cutoff = instant.date() - timedelta(days=PUBLIC_HISTORY_DAYS)
                first_week = cutoff - timedelta(days=cutoff.weekday())
                for week in _covered_weeks(
                    max(epoch.coverage_start_day, first_week), coverage_end
                ):
                    address = build_analytics_address(
                        self._public_key_hex, self._provider_d, week, epoch.epoch
                    )
                    prior_row = latest.get((epoch.epoch, address.d_tag))
                    aggregate = self._next_aggregate(
                        by_week.get(week, ()),
                        epoch=epoch,
                        week=week,
                        today_utc=completed_day_limit,
                        prior_row=prior_row,
                        rows_by_id=rows_by_id,
                        max_frame_bytes=frame_limit,
                    )
                    if aggregate is None:
                        continue
                    created_at = max(
                        int(instant.timestamp()),
                        created_at_floors.get(address.d_tag, -1) + 1,
                    )
                    encoded = encode_week_event(
                        aggregate,
                        private_key_hex=self._private_key_hex,
                        provider_d=self._provider_d,
                        created_at=created_at,
                        max_frame_bytes=frame_limit,
                    )
                    if encoded.pubkey != self._public_key_hex:
                        raise IdentityMismatchError(
                            "Analytics v2 private key does not match the claimed pubkey"
                        )
                    enqueue_result = await enqueue_signed_event(
                        self._session_factory, encoded, stored_at_ms=timestamp_ms
                    )
                    created_at_floors[address.d_tag] = max(
                        created_at_floors.get(address.d_tag, -1),
                        enqueue_result.created_at,
                    )
                    produced += int(enqueue_result.inserted)
            return produced

    async def _completed_day_limit(self, today_utc: date) -> date:
        async with self._session_factory() as session:
            result = await session.exec(
                select(TerminalOutcomeWriterRun).where(
                    col(TerminalOutcomeWriterRun.status).in_(
                        ("active", "degraded", "lost")
                    )
                )
            )
            for run in result.all():
                checkpoint = run.flushed_through_ms or run.started_at_ms
                day = _utc_day_from_ms(checkpoint)
                if run.loss_day is not None:
                    day = min(day, run.loss_day)
                today_utc = min(today_utc, day)
        return today_utc

    async def _load_eligible_epochs(
        self, active_epoch_floor: int, today_utc: date
    ) -> tuple[tuple[TerminalOutcomeEpoch, ...], tuple[LedgerOutcome, ...]]:
        yesterday = today_utc - timedelta(days=1)
        async with self._session_factory() as session:
            result = await session.exec(
                select(TerminalOutcomeEpoch)
                .where(col(TerminalOutcomeEpoch.epoch) >= active_epoch_floor)
                .order_by(col(TerminalOutcomeEpoch.epoch))
            )
            epochs = tuple(
                epoch
                for epoch in result.all()
                if epoch.coverage_start_day
                <= min(epoch.coverage_end_day or yesterday, yesterday)
            )
            if not epochs:
                return (), ()
            cutoff = today_utc - timedelta(days=PUBLIC_HISTORY_DAYS)
            first_week = cutoff - timedelta(days=cutoff.weekday())
            start = max(min(epoch.coverage_start_day for epoch in epochs), first_week)
            sources = (
                col(TerminalOutcome.input_source),
                col(TerminalOutcome.output_source),
                col(TerminalOutcome.cache_read_source),
                col(TerminalOutcome.cache_creation_source),
            )
            measured = and_(
                sources[0] == "reported",
                sources[1] == "reported",
                or_(
                    sources[2] == "reported",
                    and_(
                        sources[2] == "missing",
                        col(TerminalOutcome.cache_read_input_tokens) == 0,
                    ),
                ),
                or_(
                    sources[3] == "reported",
                    and_(
                        sources[3] == "missing",
                        col(TerminalOutcome.cache_creation_input_tokens) == 0,
                    ),
                ),
            )
            totals = await session.execute(
                sa_select(
                    col(TerminalOutcome.terminal_day),
                    col(TerminalOutcome.model_identifier),
                    *sources,
                    func.count(col(TerminalOutcome.outcome_id)),
                    func.sum(TerminalOutcome.input_tokens),
                    func.sum(TerminalOutcome.output_tokens),
                    func.sum(TerminalOutcome.cache_read_input_tokens),
                    func.sum(TerminalOutcome.cache_creation_input_tokens),
                    func.sum(TerminalOutcome.revenue_msats),
                )
                .where(col(TerminalOutcome.terminal_day) >= start)
                .where(col(TerminalOutcome.terminal_day) <= yesterday)
                .group_by(
                    col(TerminalOutcome.terminal_day),
                    col(TerminalOutcome.model_identifier),
                    *sources,
                    # Missing positive cache counts cannot contaminate eligible rows.
                    measured,
                )
            )
            rows = tuple(
                LedgerOutcome(
                    terminal_day=day,
                    model_identifier=model,
                    # PostgreSQL returns SUM(bigint) as Decimal.
                    input_tokens=int(input_tokens),
                    output_tokens=int(output_tokens),
                    cache_read_input_tokens=int(cache_read_tokens),
                    cache_creation_input_tokens=int(cache_creation_tokens),
                    revenue_msats=int(revenue),
                    input_source=input_source,
                    output_source=output_source,
                    cache_read_source=cache_read_source,
                    cache_creation_source=cache_creation_source,
                    completed_requests=count,
                )
                for day, model, input_source, output_source, cache_read_source, cache_creation_source, count, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, revenue in totals.all()
            )
        return epochs, rows

    async def _load_version_state(
        self, active_epoch_floor: int, generation: int
    ) -> tuple[
        dict[tuple[int, str], AnalyticsV2Outbox],
        dict[str, int],
        dict[str, AnalyticsV2Outbox],
    ]:
        async with self._session_factory() as session:
            result = await session.exec(
                select(AnalyticsV2Outbox)
                .where(col(AnalyticsV2Outbox.pubkey) == self._public_key_hex)
                .order_by(
                    col(AnalyticsV2Outbox.created_at).desc(),
                    col(AnalyticsV2Outbox.event_id),
                )
            )
            rows = result.all()
        latest: dict[tuple[int, str], AnalyticsV2Outbox] = {}
        created_at_floors: dict[str, int] = {}
        rows_by_id: dict[str, AnalyticsV2Outbox] = {}
        for row in rows:
            created_at_floors.setdefault(row.d_tag, row.created_at)
            rows_by_id[row.event_id] = row
            if (
                row.epoch >= active_epoch_floor
                and row.delivery_generation == generation
            ):
                latest.setdefault((row.epoch, row.d_tag), row)
        return latest, created_at_floors, rows_by_id

    def _next_aggregate(
        self,
        outcomes: Sequence[LedgerOutcome],
        *,
        epoch: TerminalOutcomeEpoch,
        week: date,
        today_utc: date,
        prior_row: AnalyticsV2Outbox | None,
        rows_by_id: dict[str, AnalyticsV2Outbox],
        max_frame_bytes: int,
    ) -> WeeklyAggregate | None:
        fresh = aggregate_ledger_week(
            outcomes,
            epoch=epoch.epoch,
            epoch_coverage_start=epoch.coverage_start_day,
            epoch_coverage_end=epoch.coverage_end_day,
            week=week,
            today_utc=today_utc,
        )
        if fresh is None or prior_row is None:
            return fresh

        latest = prior_version_from_frame(bytes(prior_row.frame))
        if fresh.through < latest.through:
            return None
        if (
            not _published_days_changed(fresh, latest)
            and fresh.through == latest.through
            and fresh.complete == latest.complete
            and len(prior_row.frame) <= max_frame_bytes
        ):
            return None
        published_row = (
            prior_row
            if prior_row.first_send_attempt_at_ms is not None
            else _latest_attempted_predecessor(prior_row, rows_by_id.values())
        )
        if published_row is None:
            return fresh
        prior = prior_version_from_frame(bytes(published_row.frame))
        return aggregate_ledger_week(
            outcomes,
            epoch=epoch.epoch,
            epoch_coverage_start=epoch.coverage_start_day,
            epoch_coverage_end=epoch.coverage_end_day,
            week=week,
            today_utc=today_utc,
            prior_version=prior,
            correction=(
                _published_days_changed(fresh, prior)
                or fresh.through == prior.through
                or len(published_row.frame) > max_frame_bytes
            ),
        )


class AnalyticsV2Delivery:
    """Deliver persisted frames with relay acceptance and exact event readback."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        operator_relays: Sequence[str],
        retry_seconds: float = 60.0,
        timeout_seconds: float = 10.0,
        sender: RelaySender | None = None,
        relay_limit_reader: RelayLimitReader = fetch_relay_max_message_length,
    ) -> None:
        if retry_seconds < 0 or timeout_seconds <= 0:
            raise AnalyticsV2DeliveryError("Delivery timing is invalid")
        self._session_factory = session_factory
        relays = sorted({_normalize_public_wss_url(url) for url in operator_relays})
        if not relays:
            raise AnalyticsV2DeliveryError("At least one analytics relay is required")
        self._targets = tuple(RelayTarget(url) for url in relays)
        self._quorum = min(ANALYTICS_RELAY_QUORUM, len(self._targets))
        self._retry_ms = int(retry_seconds * 1000)
        self._timeout_seconds = timeout_seconds
        self._sender = sender or self._send_to_relay
        self._relay_limit_reader = relay_limit_reader
        self._accepting = True
        self._active: set[asyncio.Task[RelaySendResult]] = set()
        self._relay_limits: dict[str, int | None] = {}

    async def frame_limit(self) -> int:
        self._relay_limits.clear()
        await self._load_relay_limits(self._targets)
        limits = sorted(
            (
                limit if limit is not None else DEFAULT_MAX_FRAME_BYTES
                for limit in self._relay_limits.values()
            ),
            reverse=True,
        )
        return min(DEFAULT_MAX_FRAME_BYTES, limits[self._quorum - 1])

    async def _load_relay_limits(self, targets: Sequence[RelayTarget]) -> None:
        async def read_limit(target: RelayTarget) -> int | None:
            try:
                value = await self._relay_limit_reader(target, self._timeout_seconds)
                return value if type(value) is int and value > 0 else None
            except asyncio.CancelledError:
                raise
            except Exception:
                return None

        missing = [target for target in targets if target.url not in self._relay_limits]
        limits = await asyncio.gather(*(read_limit(target) for target in missing))
        self._relay_limits.update(
            (target.url, limit) for target, limit in zip(missing, limits)
        )

    async def deliver_pending_once(
        self, *, at_ms: int | None = None
    ) -> DeliveryPassResult:
        timestamp = _clock_ms() if at_ms is None else at_ms
        _require_nonnegative_int(timestamp, "at_ms")
        if not self._accepting:
            return DeliveryPassResult(0, 0)
        state = await get_analytics_v2_delivery_state(
            self._session_factory, at_ms=timestamp
        )
        if not state.sharing_enabled:
            return DeliveryPassResult(0, 0)

        async with self._session_factory() as session:
            result = await session.exec(
                select(AnalyticsV2Outbox)
                .where(col(AnalyticsV2Outbox.status) == "pending")
                .where(col(AnalyticsV2Outbox.delivery_generation) == state.generation)
                .where(col(AnalyticsV2Outbox.next_attempt_at_ms) <= timestamp)
                .order_by(
                    col(AnalyticsV2Outbox.created_at),
                    col(AnalyticsV2Outbox.event_id),
                )
            )
            event_ids = [row.event_id for row in result.all()]

        attempted = 0
        delivered = 0
        for event_id in event_ids:
            if not self._accepting:
                break
            outcome = await self._deliver_event(event_id, timestamp)
            if outcome is None:
                continue
            attempted += 1
            delivered += int(outcome)
        self._relay_limits.clear()
        return DeliveryPassResult(attempted, delivered)

    async def disable(self, *, at_ms: int | None = None) -> DeliveryStateSnapshot:
        self._accepting = False
        await self._cancel_active()
        return await transition_analytics_v2_sharing(
            self._session_factory, enabled=False, at_ms=at_ms
        )

    async def resume(self) -> bool:
        state = await get_analytics_v2_delivery_state(self._session_factory)
        self._accepting = state.sharing_enabled
        return self._accepting

    async def stop(self) -> None:
        self._accepting = False
        await self._cancel_active()

    async def _cancel_active(self) -> None:
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _deliver_event(self, event_id: str, timestamp: int) -> bool | None:
        pending = await self._load_pending_frame(event_id)
        if pending is None:
            return None
        targets = self._targets
        async with self._session_factory() as session:
            receipt_result = await session.exec(
                select(AnalyticsV2RelayReceipt.relay_url).where(
                    col(AnalyticsV2RelayReceipt.event_id) == event_id
                )
            )
            completed_urls = set(receipt_result.all())
        pending_targets = [
            target for target in targets if target.url not in completed_urls
        ]
        eligible_targets: Sequence[RelayTarget] = ()
        if pending_targets:
            eligible_targets = await self._eligible_targets(
                pending_targets, pending.frame
            )
        if not eligible_targets:
            reconciled = await self._finish_attempt(event_id, _clock_ms())
            return True if reconciled else None
        row = await self._begin_attempt(event_id, timestamp)
        if row is None or row != pending:
            return None

        async def is_active() -> bool:
            return self._accepting and await self._attempt_is_active(row)

        async def send(target: RelayTarget) -> RelaySendResult:
            if not await is_active():
                return RelaySendResult(False, False)
            return await self._sender(target, event_id, bytes(row.frame), is_active)

        tasks: list[tuple[RelayTarget, asyncio.Task[RelaySendResult]]] = []
        for target in eligible_targets:
            task: asyncio.Task[RelaySendResult] = asyncio.create_task(send(target))
            self._active.add(task)
            task.add_done_callback(self._active.discard)
            tasks.append((target, task))
        try:
            results = await asyncio.gather(
                *(task for _, task in tasks), return_exceptions=True
            )
        except asyncio.CancelledError:
            for _, task in tasks:
                task.cancel()
            raise
        if not self._accepting or not await self._attempt_is_active(row):
            return None
        for (target, _), send_result in zip(tasks, results):
            if (
                isinstance(send_result, RelaySendResult)
                and send_result.accepted
                and send_result.read_back
            ):
                await self._record_receipt(event_id, target, _clock_ms())
        return await self._finish_attempt(event_id, _clock_ms())

    async def _load_pending_frame(self, event_id: str) -> _PendingFrame | None:
        async with self._session_factory() as session:
            state = await session.get(AnalyticsV2DeliveryState, 1)
            row = await session.get(AnalyticsV2Outbox, event_id)
        if (
            not self._accepting
            or state is None
            or not state.sharing_enabled
            or row is None
            or row.status != "pending"
            or row.delivery_generation != state.generation
        ):
            return None
        return _PendingFrame(row.event_id, bytes(row.frame), row.delivery_generation)

    async def _eligible_targets(
        self, targets: Sequence[RelayTarget], frame: bytes
    ) -> tuple[RelayTarget, ...]:
        await self._load_relay_limits(targets)
        return tuple(
            target
            for target in targets
            if len(frame) <= (self._relay_limits[target.url] or DEFAULT_MAX_FRAME_BYTES)
        )

    async def _begin_attempt(
        self, event_id: str, timestamp: int
    ) -> _PendingFrame | None:
        async with self._session_factory() as session:
            state = await _get_or_create_delivery_state(session, timestamp)
            row = await session.get(AnalyticsV2Outbox, event_id)
            if (
                not self._accepting
                or not state.sharing_enabled
                or row is None
                or row.status != "pending"
                or row.delivery_generation != state.generation
            ):
                await session.commit()
                return None
            row.attempt_count += 1
            row.next_attempt_at_ms = timestamp + self._retry_ms
            if row.first_send_attempt_at_ms is None:
                row.first_send_attempt_at_ms = timestamp
            pending = _PendingFrame(
                row.event_id, bytes(row.frame), row.delivery_generation
            )
            await session.commit()
            return pending

    async def _attempt_is_active(self, pending: _PendingFrame) -> bool:
        async with self._session_factory() as session:
            state = await session.get(AnalyticsV2DeliveryState, 1)
            row = await session.get(AnalyticsV2Outbox, pending.event_id)
        return bool(
            state is not None
            and state.sharing_enabled
            and state.generation == pending.delivery_generation
            and row is not None
            and row.status == "pending"
            and row.delivery_generation == pending.delivery_generation
        )

    async def _record_receipt(
        self, event_id: str, target: RelayTarget, timestamp: int
    ) -> None:
        key = (event_id, target.url)
        async with self._session_factory() as session:
            receipt = await session.get(AnalyticsV2RelayReceipt, key)
            if receipt is None:
                session.add(
                    AnalyticsV2RelayReceipt(
                        event_id=event_id,
                        relay_url=target.url,
                        accepted_at_ms=timestamp,
                        read_back_at_ms=timestamp,
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()

    async def _finish_attempt(self, event_id: str, timestamp: int) -> bool:
        async with self._session_factory() as session:
            state = await session.get(AnalyticsV2DeliveryState, 1)
            row = await session.get(AnalyticsV2Outbox, event_id)
            result = await session.exec(
                select(AnalyticsV2RelayReceipt.relay_url).where(
                    col(AnalyticsV2RelayReceipt.event_id) == event_id
                )
            )
            accepted_urls = set(result.all()) & {target.url for target in self._targets}
            healthy = bool(
                state is not None
                and state.sharing_enabled
                and row is not None
                and row.status == "pending"
                and row.delivery_generation == state.generation
                and len(accepted_urls) >= self._quorum
            )
            if row is not None and row.status == "pending" and healthy:
                row.status = "delivered"
                row.delivered_at_ms = timestamp
            await session.commit()
            return healthy

    async def _send_to_relay(
        self,
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        return await publish_frame_to_relay(
            target,
            event_id,
            frame,
            is_active=is_active,
            timeout_seconds=self._timeout_seconds,
        )


async def publish_frame_to_relay(
    target: RelayTarget,
    event_id: str,
    frame: bytes,
    *,
    is_active: Callable[[], Awaitable[bool]] | None = None,
    timeout_seconds: float = 10.0,
) -> RelaySendResult:
    """Send an exact persisted frame, then require OK and exact-id readback."""
    prior = prior_version_from_frame(frame)
    if prior.event_id != event_id:
        raise OutboxConflictError("Relay event ID does not match its signed frame")
    frame_text = frame.decode("utf-8")
    expected_event = json.loads(frame_text)[1]
    subscription_id = uuid.uuid4().hex
    try:
        endpoint = await resolve_public_relay_endpoint(target.url)
        connection = websockets.connect(
            target.url,
            open_timeout=timeout_seconds,
            close_timeout=timeout_seconds,
            host=endpoint.address,
            port=endpoint.port,
            server_hostname=endpoint.server_hostname,
        )
        connection.MAX_REDIRECTS_ALLOWED = 1
        async with asyncio.timeout(timeout_seconds):
            async with connection as websocket:
                if is_active is not None and not await is_active():
                    return RelaySendResult(False, False)
                await websocket.send(frame_text)
                accepted = False
                while True:
                    parsed = parse_relay_ok(await websocket.recv(), event_id)
                    if parsed is None:
                        continue
                    if not parsed:
                        return RelaySendResult(False, False)
                    accepted = True
                    break

                request = json.dumps(
                    ["REQ", subscription_id, {"ids": [event_id]}],
                    separators=(",", ":"),
                )
                await websocket.send(request)
                read_back = False
                while True:
                    message = _decode_relay_message(await websocket.recv())
                    if (
                        isinstance(message, list)
                        and len(message) == 3
                        and message[0] == "EVENT"
                        and message[1] == subscription_id
                        and isinstance(message[2], dict)
                        and message[2] == expected_event
                    ):
                        read_back = True
                        break
                    elif (
                        isinstance(message, list)
                        and len(message) >= 2
                        and message[0] == "EOSE"
                        and message[1] == subscription_id
                    ):
                        break
                await websocket.send(
                    json.dumps(["CLOSE", subscription_id], separators=(",", ":"))
                )
                return RelaySendResult(accepted, read_back)
    except asyncio.CancelledError:
        raise
    except Exception:
        return RelaySendResult(False, False)


def parse_relay_ok(message: str | bytes, event_id: str) -> bool | None:
    """Return the exact relay decision, or None for an unrelated frame."""
    try:
        parsed = _decode_relay_message(message)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if (
        not isinstance(parsed, list)
        or len(parsed) < 3
        or parsed[0] != "OK"
        or parsed[1] != event_id
    ):
        return None
    if parsed[2] is True:
        return True
    if parsed[2] is False:
        return False
    return None


async def resolve_public_relay_endpoint(relay_url: str) -> ResolvedRelayEndpoint:
    """Resolve once, reject any private answer, and pin the socket destination."""
    normalized = _normalize_public_wss_url(relay_url)
    parsed = urlsplit(normalized)
    hostname = parsed.hostname
    if hostname is None:
        raise AnalyticsV2DeliveryError("Relay URL hostname is empty")
    port = parsed.port or 443
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as error:
        raise AnalyticsV2DeliveryError("Relay hostname did not resolve") from error
    addresses = {ipaddress.ip_address(answer[4][0]) for answer in answers if answer[4]}
    if not addresses or any(not address.is_global for address in addresses):
        raise AnalyticsV2DeliveryError("Relay hostname resolved to a non-public IP")
    selected = sorted(
        addresses, key=lambda address: (address.version, address.compressed)
    )[0]
    return ResolvedRelayEndpoint(selected.compressed, port, hostname)


async def run_analytics_v2_publisher(
    producer: AnalyticsV2Producer,
    delivery: AnalyticsV2Delivery,
    *,
    interval_seconds: float = 300.0,
) -> None:
    """Run cancellation-safe production and background delivery."""
    if interval_seconds <= 0:
        raise AnalyticsV2DeliveryError("Publisher interval must be positive")
    try:
        while True:
            state = await get_analytics_v2_delivery_state(producer.session_factory)
            if state.sharing_enabled:
                try:
                    await producer.produce_once(
                        max_frame_bytes=await delivery.frame_limit()
                    )
                    await delivery.deliver_pending_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Analytics v2 publisher pass failed")
            await asyncio.sleep(interval_seconds)
    finally:
        await delivery.stop()


async def _get_or_create_delivery_state(
    session: AsyncSession, timestamp: int
) -> AnalyticsV2DeliveryState:
    state = await session.get(AnalyticsV2DeliveryState, 1)
    if state is None:
        state = AnalyticsV2DeliveryState(id=1, updated_at_ms=timestamp)
        session.add(state)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            state = await session.get(AnalyticsV2DeliveryState, 1)
            if state is None:
                raise
    return state


async def _close_current_epoch(
    session: AsyncSession, *, coverage_end_day: date
) -> None:
    result = await session.exec(
        select(TerminalOutcomeEpoch)
        .where(col(TerminalOutcomeEpoch.current_slot) == 1)
        .with_for_update()
    )
    current = result.first()
    if current is None:
        return
    if coverage_end_day < current.coverage_start_day:
        outbox_result = await session.exec(
            select(AnalyticsV2Outbox.event_id)
            .where(col(AnalyticsV2Outbox.epoch) == current.epoch)
            .limit(1)
        )
        if outbox_result.first() is not None:
            raise AnalyticsV2DeliveryError(
                "Cannot discard an empty epoch with a durable outbox event"
            )
        await session.delete(current)
        return
    current.coverage_end_day = coverage_end_day
    current.current_slot = None


def _snapshot(state: AnalyticsV2DeliveryState) -> DeliveryStateSnapshot:
    return DeliveryStateSnapshot(
        sharing_enabled=state.sharing_enabled,
        generation=state.generation,
        active_epoch_floor=state.active_epoch_floor,
        identity_pubkey=state.identity_pubkey,
        provider_d=state.provider_d,
        updated_at_ms=state.updated_at_ms,
    )


def _validate_encoded_metadata(
    event: EncodedAnalyticsEvent, parsed: PriorVersion
) -> None:
    expected = (
        event.event_id,
        event.pubkey,
        event.d_tag,
        event.created_at,
        event.week,
        event.epoch,
        event.coverage_start,
        event.through,
        event.complete,
        event.corrected,
        event.days,
        event.daily_models,
    )
    actual = (
        parsed.event_id,
        parsed.pubkey,
        parsed.d_tag,
        parsed.created_at,
        parsed.week,
        parsed.epoch,
        parsed.coverage_start,
        parsed.through,
        parsed.complete,
        parsed.corrected,
        parsed.days,
        parsed.daily_models,
    )
    if expected != actual:
        raise OutboxConflictError("Encoded metadata does not match the signed frame")


def _require_exact_outbox(
    row: AnalyticsV2Outbox,
    event: EncodedAnalyticsEvent,
    generation: int,
    semantic_slot: str,
) -> None:
    if row.semantic_slot not in {
        semantic_slot,
        f"superseded:{row.event_id}",
    }:
        raise OutboxConflictError("Event id has conflicting outbox data")
    stored_prior = prior_version_from_frame(bytes(row.frame))
    if stored_prior.event_id != event.event_id:
        raise OutboxConflictError("Event id has conflicting outbox data")
    existing = (
        row.event_id,
        row.pubkey,
        row.d_tag,
        row.kind,
        row.week,
        row.epoch,
        row.through_day,
        row.delivery_generation,
        json.loads(bytes(row.frame))[1]["content"],
        row.finalized,
        row.corrected,
        row.created_at,
    )
    candidate = (
        event.event_id,
        event.pubkey,
        event.d_tag,
        ANALYTICS_KIND,
        event.week,
        event.epoch,
        event.through,
        generation,
        event.content.decode("utf-8"),
        event.complete,
        event.corrected,
        event.created_at,
    )
    if existing != candidate:
        raise OutboxConflictError("Event id has conflicting outbox data")


def _require_semantic_winner(
    row: AnalyticsV2Outbox,
    event: EncodedAnalyticsEvent,
    generation: int,
    semantic_slot: str,
) -> None:
    parsed = prior_version_from_frame(bytes(row.frame))
    frame = json.loads(bytes(row.frame))
    stored_content = frame[1]["content"].encode("utf-8")
    existing = (
        row.pubkey,
        row.d_tag,
        row.week,
        row.epoch,
        row.through_day,
        row.semantic_slot,
        row.delivery_generation,
        row.finalized,
        row.corrected,
        parsed.days,
        stored_content,
    )
    candidate = (
        event.pubkey,
        event.d_tag,
        event.week,
        event.epoch,
        event.through,
        semantic_slot,
        generation,
        event.complete,
        event.corrected,
        event.days,
        event.content,
    )
    if existing != candidate:
        raise OutboxConflictError("Semantic slot has conflicting signed content")


def _semantic_slot(event: EncodedAnalyticsEvent) -> str:
    payload = json.loads(event.content)
    corrects = payload.get("corrects")
    if isinstance(corrects, str):
        return f"correction:{corrects}"
    return f"ordinary:{event.epoch}:{event.through.isoformat()}"


def _published_days_changed(current: WeeklyAggregate, prior: PriorVersion) -> bool:
    current_days = {row.day: row.values for row in current.days}
    return (
        current.coverage_start != prior.coverage_start
        or current.epoch != prior.epoch
        or any(current_days.get(row.day) != row.values for row in prior.days)
        or daily_models_changed(current, prior)
    )


def _latest_attempted_predecessor(
    row: AnalyticsV2Outbox, versions: Iterable[AnalyticsV2Outbox]
) -> AnalyticsV2Outbox | None:
    candidates = [
        candidate
        for candidate in versions
        if candidate.epoch == row.epoch
        and candidate.d_tag == row.d_tag
        and candidate.delivery_generation == row.delivery_generation
        and candidate.first_send_attempt_at_ms is not None
    ]
    if not candidates:
        return None
    return min(
        candidates, key=lambda candidate: (-candidate.created_at, candidate.event_id)
    )


def _covered_weeks(coverage_start: date, coverage_end: date) -> tuple[date, ...]:
    if coverage_start > coverage_end:
        return ()
    first = coverage_start - timedelta(days=coverage_start.weekday())
    last = coverage_end - timedelta(days=coverage_end.weekday())
    return tuple(
        first + timedelta(days=7 * offset)
        for offset in range(((last - first).days // 7) + 1)
    )


def _validate_identity(pubkey: str, provider_d: str) -> None:
    _require_lower_hex(pubkey, 64, "analytics pubkey")
    if not isinstance(provider_d, str) or not 1 <= len(provider_d) <= 64:
        raise IdentityMismatchError("provider_d must contain 1 to 64 characters")
    if any(unicodedata.category(character) == "Cc" for character in provider_d):
        raise IdentityMismatchError("provider_d contains control characters")


def _require_nonnegative_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnalyticsV2DeliveryError(f"{name} must be a non-negative integer")


def _decode_relay_message(message: str | bytes) -> Any:
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    return json.loads(message)


def _clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _utc_day_from_ms(timestamp: int) -> date:
    return datetime.fromtimestamp(timestamp / 1000, UTC).date()
