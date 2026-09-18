from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import (
    AnalyticsV2Outbox,
    AnalyticsV2RelayReceipt,
    TerminalOutcomeEpoch,
)
from routstr.nostr import analytics_v2_delivery as delivery_module
from routstr.nostr.analytics_v2 import (
    EncodedAnalyticsEvent,
    LedgerOutcome,
    aggregate_ledger_week,
    encode_week_event,
)
from routstr.nostr.analytics_v2_delivery import (
    ActivationResult,
    AnalyticsV2Delivery,
    AnalyticsV2DeliveryError,
    OutboxConflictError,
    RelaySendResult,
    RelayTarget,
    ResolvedRelayEndpoint,
    SharingDisabledError,
    activate_analytics_v2_sharing,
    claim_analytics_v2_identity,
    enqueue_signed_event,
    fetch_relay_max_message_length,
    get_analytics_v2_delivery_state,
    parse_relay_ok,
    publish_frame_to_relay,
    resolve_public_relay_endpoint,
    rotate_analytics_v2_identity,
    transition_analytics_v2_sharing,
)

PRIVATE_KEY = "11" * 32
OTHER_PRIVATE_KEY = "22" * 32
PUBLIC_KEY = Keys.parse(PRIVATE_KEY).public_key().to_hex()
OTHER_PUBLIC_KEY = Keys.parse(OTHER_PRIVATE_KEY).public_key().to_hex()
WEEK = date(2026, 8, 31)
RELAYS = (
    "wss://relay-a.valid.net",
    "wss://relay-b.valid.net",
    "wss://relay-c.valid.net",
)


def _at_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


async def _no_advertised_limit(_target: RelayTarget, _timeout_seconds: float) -> None:
    return None


@pytest_asyncio.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delivery.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _activate(
    factory: async_sessionmaker[AsyncSession],
) -> ActivationResult:
    assert (
        await claim_analytics_v2_identity(
            factory, pubkey=PUBLIC_KEY, provider_d="provider", at_ms=1
        )
        == "initialized"
    )
    return await activate_analytics_v2_sharing(
        factory, coverage_day=WEEK - timedelta(days=1), at_ms=2
    )


def _open_versions() -> tuple[EncodedAnalyticsEvent, EncodedAnalyticsEvent]:
    first_aggregate = aggregate_ledger_week(
        [],
        epoch=0,
        epoch_coverage_start=WEEK,
        epoch_coverage_end=None,
        week=WEEK,
        today_utc=WEEK + timedelta(days=1),
    )
    assert first_aggregate is not None
    first = encode_week_event(
        first_aggregate,
        private_key_hex=PRIVATE_KEY,
        provider_d="provider",
        created_at=100,
    )
    second_aggregate = aggregate_ledger_week(
        [],
        epoch=0,
        epoch_coverage_start=WEEK,
        epoch_coverage_end=None,
        week=WEEK,
        today_utc=WEEK + timedelta(days=2),
        prior_version=first.as_prior_version(),
    )
    assert second_aggregate is not None
    second = encode_week_event(
        second_aggregate,
        private_key_hex=PRIVATE_KEY,
        provider_d="provider",
        created_at=101,
    )
    return first, second


@pytest.mark.asyncio
async def test_activation_rotates_continuity_in_same_idempotent_transition(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    activation = await _activate(session_factory)
    assert activation.transitioned is True
    assert activation.state.sharing_enabled is True
    assert activation.state.generation == 1

    repeated = await activate_analytics_v2_sharing(
        session_factory, coverage_day=WEEK, at_ms=3
    )
    assert repeated.transitioned is False
    assert repeated.state.generation == 1
    async with session_factory() as session:
        result = await session.exec(
            select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
        )
        epochs = result.all()
    assert [
        (row.epoch, row.coverage_start_day, row.coverage_end_day) for row in epochs
    ] == [
        (0, WEEK, None),
    ]

    with pytest.raises(AnalyticsV2DeliveryError, match="activate_analytics_v2"):
        disabled = await transition_analytics_v2_sharing(
            session_factory,
            enabled=False,
            at_ms=_at_ms(WEEK + timedelta(days=2)),
        )
        assert disabled.sharing_enabled is False
        await transition_analytics_v2_sharing(session_factory, enabled=True, at_ms=5)
    reenabled = await activate_analytics_v2_sharing(
        session_factory, coverage_day=WEEK + timedelta(days=4), at_ms=6
    )
    assert reenabled.transitioned is True
    assert reenabled.state.generation == 3
    async with session_factory() as session:
        result = await session.exec(
            select(TerminalOutcomeEpoch).order_by(col(TerminalOutcomeEpoch.epoch))
        )
        epochs = result.all()
    assert [
        (row.epoch, row.coverage_start_day, row.coverage_end_day) for row in epochs
    ] == [
        # Opting out kept private coverage open until the next activation.
        (0, WEEK, WEEK + timedelta(days=3)),
        (1, WEEK + timedelta(days=5), None),
    ]


@pytest.mark.asyncio
async def test_activation_on_flags_older_than_an_opt_out_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seen = (await _activate(session_factory)).state.generation
    await transition_analytics_v2_sharing(session_factory, enabled=False, at_ms=3)

    with pytest.raises(SharingDisabledError):
        await activate_analytics_v2_sharing(
            session_factory, coverage_day=WEEK, at_ms=4, expected_generation=seen
        )
    state = await get_analytics_v2_delivery_state(session_factory)
    assert not state.sharing_enabled


@pytest.mark.asyncio
async def test_concurrent_activation_rotates_exactly_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await claim_analytics_v2_identity(
        session_factory,
        pubkey=PUBLIC_KEY,
        provider_d="provider",
        at_ms=1,
    )
    results = await asyncio.gather(
        activate_analytics_v2_sharing(
            session_factory, coverage_day=WEEK - timedelta(days=1), at_ms=2
        ),
        activate_analytics_v2_sharing(
            session_factory, coverage_day=WEEK - timedelta(days=1), at_ms=2
        ),
    )
    assert sorted(result.transitioned for result in results) == [False, True]
    async with session_factory() as session:
        epochs = (await session.exec(select(TerminalOutcomeEpoch))).all()
    assert len(epochs) == 1
    assert sum(epoch.current_slot == 1 for epoch in epochs) == 1


@pytest.mark.asyncio
async def test_first_activation_adopts_writer_created_epoch_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(
            TerminalOutcomeEpoch(
                epoch=0,
                coverage_start_day=WEEK,
                current_slot=1,
            )
        )
        await session.commit()
    await claim_analytics_v2_identity(
        session_factory,
        pubkey=PUBLIC_KEY,
        provider_d="provider",
        at_ms=1,
    )

    activation = await activate_analytics_v2_sharing(
        session_factory,
        coverage_day=WEEK - timedelta(days=1),
        at_ms=2,
    )

    assert activation.transitioned is True
    assert activation.state.active_epoch_floor == 0
    async with session_factory() as session:
        epochs = (await session.exec(select(TerminalOutcomeEpoch))).all()
    assert [
        (
            epoch.epoch,
            epoch.coverage_start_day,
            epoch.coverage_end_day,
            epoch.current_slot,
        )
        for epoch in epochs
    ] == [(0, WEEK, None, 1)]


@pytest.mark.asyncio
async def test_concurrent_identity_claim_initializes_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    results = await asyncio.gather(
        claim_analytics_v2_identity(
            session_factory,
            pubkey=PUBLIC_KEY,
            provider_d="provider",
            at_ms=1,
        ),
        claim_analytics_v2_identity(
            session_factory,
            pubkey=OTHER_PUBLIC_KEY,
            provider_d="other",
            at_ms=2,
        ),
    )
    assert sorted(results) == ["initialized", "mismatch"]
    state = await get_analytics_v2_delivery_state(session_factory)
    assert (state.identity_pubkey, state.provider_d) in {
        (PUBLIC_KEY, "provider"),
        (OTHER_PUBLIC_KEY, "other"),
    }


@pytest.mark.asyncio
async def test_dns_resolution_rejects_private_answers_and_pins_public_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def private_answer(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(delivery_module.socket, "getaddrinfo", private_answer)
    with pytest.raises(AnalyticsV2DeliveryError, match="non-public"):
        await resolve_public_relay_endpoint("wss://relay.valid.net")

    def public_answer(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(delivery_module.socket, "getaddrinfo", public_answer)
    assert await resolve_public_relay_endpoint("wss://relay.valid.net") == (
        ResolvedRelayEndpoint("8.8.8.8", 443, "relay.valid.net")
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('["OK","event",true,""]', True),
        ('["OK","event",false,"blocked"]', False),
        ('["OK","other",true,""]', None),
        ('["OK","event",1,""]', None),
        ('["NOTICE","event",true]', None),
    ],
)
def test_parse_relay_ok_requires_exact_id_and_boolean(
    payload: str, expected: bool | None
) -> None:
    assert parse_relay_ok(payload, "event") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("readback_kind", ["exact", "wrong_id", "forged_content"])
async def test_websocket_send_preserves_frame_and_requires_exact_readback(
    monkeypatch: pytest.MonkeyPatch,
    readback_kind: str,
) -> None:
    sent: list[str] = []
    encoded, _ = _open_versions()
    event_id = encoded.event_id
    returned_event = dict(encoded.event)
    if readback_kind == "wrong_id":
        returned_event["id"] = "00" * 32
    elif readback_kind == "forged_content":
        returned_event["content"] = "{}"

    class FakeWebSocket:
        receive_count = 0

        async def __aenter__(self) -> FakeWebSocket:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def send(self, message: str) -> None:
            sent.append(message)

        async def recv(self) -> str:
            self.receive_count += 1
            if self.receive_count == 1:
                return json.dumps(["OK", event_id, True, ""])
            subscription_id = json.loads(sent[1])[1]
            if self.receive_count == 2:
                return json.dumps(
                    ["EVENT", subscription_id, returned_event],
                    separators=(",", ":"),
                )
            return json.dumps(["EOSE", subscription_id], separators=(",", ":"))

    async def resolved(url: str) -> ResolvedRelayEndpoint:
        return ResolvedRelayEndpoint("8.8.8.8", 443, "relay.valid.net")

    monkeypatch.setattr(delivery_module, "resolve_public_relay_endpoint", resolved)
    monkeypatch.setattr(
        delivery_module.websockets,
        "connect",
        lambda *args, **kwargs: FakeWebSocket(),
    )
    frame = encoded.frame
    result = await publish_frame_to_relay(
        RelayTarget("wss://relay.valid.net"),
        event_id,
        frame,
    )
    assert sent[0].encode() == frame
    assert result == RelaySendResult(True, readback_kind == "exact")


@pytest.mark.asyncio
async def test_nip11_lookup_is_pinned_bounded_and_disables_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maximum = delivery_module.NIP11_MAX_DOCUMENT_BYTES
    valid_body = json.dumps({"limitation": {"max_message_length": 123}}).encode()
    responses = [
        (200, valid_body, len(valid_body)),
        (302, valid_body, len(valid_body)),
        (200, valid_body, maximum + 1),
        (200, b"x" * (maximum + 1), None),
    ]
    requests: list[tuple[str, dict[str, Any]]] = []
    connector_arguments: list[dict[str, Any]] = []
    read_sizes: list[int] = []

    class FakeContent:
        def __init__(self, body: bytes) -> None:
            self._body = body

        async def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return self._body

    class FakeResponse:
        def __init__(
            self, status: int, body: bytes, content_length: int | None
        ) -> None:
            self.status = status
            self.content_length = content_length
            self.content = FakeContent(body)

        async def __aenter__(self) -> FakeResponse:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeSession:
        def __init__(self, **kwargs: Any) -> None:
            self._arguments = kwargs

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def get(self, url: str, **kwargs: Any) -> FakeResponse:
            requests.append((url, kwargs))
            return FakeResponse(*responses.pop(0))

    async def resolved(_url: str) -> ResolvedRelayEndpoint:
        return ResolvedRelayEndpoint("8.8.8.8", 443, "relay.valid.net")

    def connector(**kwargs: Any) -> object:
        connector_arguments.append(kwargs)
        return object()

    monkeypatch.setattr(delivery_module, "resolve_public_relay_endpoint", resolved)
    monkeypatch.setattr(delivery_module.aiohttp, "TCPConnector", connector)
    monkeypatch.setattr(delivery_module.aiohttp, "ClientSession", FakeSession)
    target = RelayTarget("wss://relay.valid.net/path")

    assert await fetch_relay_max_message_length(target, 1) == 123
    assert await fetch_relay_max_message_length(target, 1) is None
    assert await fetch_relay_max_message_length(target, 1) is None
    assert await fetch_relay_max_message_length(target, 1) is None
    assert all(url == "https://relay.valid.net/path" for url, _ in requests)
    assert all(
        request["allow_redirects"] is False
        and request["headers"] == {"Accept": "application/nostr+json"}
        for _, request in requests
    )
    assert read_sizes == [maximum + 1, maximum + 1]

    resolver = connector_arguments[0]["resolver"]
    resolved_addresses = await resolver.resolve("relay.valid.net", 443)
    assert resolved_addresses[0]["host"] == "8.8.8.8"
    with pytest.raises(OSError, match="changed hostname"):
        await resolver.resolve("127.0.0.1", 443)


@pytest.mark.asyncio
async def test_delivery_applies_nip11_limit_to_exact_persisted_frame_bytes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    limits = {
        "wss://relay-a.valid.net": len(first.frame) - 1,
        "wss://relay-b.valid.net": len(first.frame),
        "wss://relay-c.valid.net": None,
    }
    sends: list[str] = []

    async def relay_limit_reader(
        target: RelayTarget, _timeout_seconds: float
    ) -> int | None:
        return limits[target.url]

    async def sender(
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        assert event_id == first.event_id
        assert frame == first.frame
        sends.append(target.url)
        return RelaySendResult(True, True)

    delivery = AnalyticsV2Delivery(
        session_factory,
        operator_relays=RELAYS,
        sender=sender,
        relay_limit_reader=relay_limit_reader,
    )
    assert await delivery.deliver_pending_once(at_ms=4) == (
        delivery_module.DeliveryPassResult(1, 1)
    )
    assert set(sends) == {
        "wss://relay-b.valid.net",
        "wss://relay-c.valid.net",
    }
    async with session_factory() as session:
        receipts = (
            await session.exec(
                select(AnalyticsV2RelayReceipt).where(
                    col(AnalyticsV2RelayReceipt.event_id) == first.event_id
                )
            )
        ).all()
    assert {receipt.relay_url for receipt in receipts} == set(sends)


@pytest.mark.asyncio
async def test_all_nip11_ineligible_relays_do_not_record_a_send_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    sends = 0

    async def relay_limit_reader(_target: RelayTarget, _timeout_seconds: float) -> int:
        return len(first.frame) - 1

    async def sender(
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        nonlocal sends
        sends += 1
        return RelaySendResult(True, True)

    delivery = AnalyticsV2Delivery(
        session_factory,
        operator_relays=RELAYS,
        sender=sender,
        relay_limit_reader=relay_limit_reader,
    )
    assert await delivery.deliver_pending_once(at_ms=4) == (
        delivery_module.DeliveryPassResult(0, 0)
    )
    async with session_factory() as session:
        row = await session.get(AnalyticsV2Outbox, first.event_id)
        receipts = (
            await session.exec(
                select(AnalyticsV2RelayReceipt).where(
                    col(AnalyticsV2RelayReceipt.event_id) == first.event_id
                )
            )
        ).all()
    assert sends == 0
    assert row is not None and row.attempt_count == 0
    assert row.first_send_attempt_at_ms is None
    assert receipts == []


@pytest.mark.asyncio
async def test_nip11_ineligible_remainder_reconciles_durable_quorum(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    async with session_factory() as session:
        row = await session.get(AnalyticsV2Outbox, first.event_id)
        assert row is not None
        row.attempt_count = 1
        row.first_send_attempt_at_ms = 3
        session.add_all(
            [
                AnalyticsV2RelayReceipt(
                    event_id=first.event_id,
                    relay_url="wss://relay-a.valid.net",
                    accepted_at_ms=3,
                    read_back_at_ms=3,
                ),
                AnalyticsV2RelayReceipt(
                    event_id=first.event_id,
                    relay_url="wss://relay-c.valid.net",
                    accepted_at_ms=3,
                    read_back_at_ms=3,
                ),
            ]
        )
        await session.commit()
    sends = 0

    async def relay_limit_reader(target: RelayTarget, _timeout_seconds: float) -> int:
        assert target.url == "wss://relay-b.valid.net"
        return len(first.frame) - 1

    async def sender(
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        nonlocal sends
        sends += 1
        return RelaySendResult(True, True)

    delivery = AnalyticsV2Delivery(
        session_factory,
        operator_relays=RELAYS,
        sender=sender,
        relay_limit_reader=relay_limit_reader,
    )
    assert await delivery.deliver_pending_once(at_ms=4) == (
        delivery_module.DeliveryPassResult(1, 1)
    )
    async with session_factory() as session:
        row = await session.get(AnalyticsV2Outbox, first.event_id)
    assert sends == 0
    assert row is not None and row.status == "delivered"
    assert row.attempt_count == 1


@pytest.mark.asyncio
async def test_delivery_commits_before_network_and_retries_exact_bytes_after_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    seen_frames: dict[str, list[bytes]] = {}
    group_b_attempts = 0

    async def sender(
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        nonlocal group_b_attempts
        async with session_factory() as session:
            row = await session.get(AnalyticsV2Outbox, event_id)
        assert row is not None and bytes(row.frame) == frame
        assert row.first_send_attempt_at_ms == 4
        assert row.attempt_count >= 1
        seen_frames.setdefault(target.url, []).append(frame)
        if target.url == RELAYS[1]:
            return RelaySendResult(False, False)
        if target.url == RELAYS[2]:
            group_b_attempts += 1
            if group_b_attempts == 1:
                return RelaySendResult(False, False)
        return RelaySendResult(True, True)

    delivery = AnalyticsV2Delivery(
        session_factory,
        operator_relays=RELAYS,
        retry_seconds=0,
        sender=sender,
        relay_limit_reader=_no_advertised_limit,
    )
    first_pass = await delivery.deliver_pending_once(at_ms=4)
    assert first_pass == delivery_module.DeliveryPassResult(1, 0)
    delivery = AnalyticsV2Delivery(
        session_factory,
        operator_relays=RELAYS,
        retry_seconds=0,
        sender=sender,
        relay_limit_reader=_no_advertised_limit,
    )
    second_pass = await delivery.deliver_pending_once(at_ms=5)
    assert second_pass == delivery_module.DeliveryPassResult(1, 1)

    assert seen_frames["wss://relay-c.valid.net"] == [first.frame, first.frame]
    async with session_factory() as session:
        row = await session.get(AnalyticsV2Outbox, first.event_id)
        receipts_result = await session.exec(
            select(AnalyticsV2RelayReceipt).where(
                col(AnalyticsV2RelayReceipt.event_id) == first.event_id
            )
        )
    assert row is not None and row.status == "delivered"
    assert {receipt.relay_url for receipt in receipts_result.all()} == {
        RELAYS[0],
        RELAYS[2],
    }


@pytest.mark.asyncio
async def test_delivery_retains_and_attempts_same_week_epochs_in_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    closed_aggregate = aggregate_ledger_week(
        [],
        epoch=0,
        epoch_coverage_start=WEEK,
        epoch_coverage_end=WEEK + timedelta(days=2),
        week=WEEK,
        today_utc=WEEK + timedelta(days=4),
    )
    current_aggregate = aggregate_ledger_week(
        [],
        epoch=1,
        epoch_coverage_start=WEEK + timedelta(days=4),
        epoch_coverage_end=None,
        week=WEEK,
        today_utc=WEEK + timedelta(days=6),
    )
    assert closed_aggregate is not None and current_aggregate is not None
    closed = encode_week_event(
        closed_aggregate,
        private_key_hex=PRIVATE_KEY,
        provider_d="provider",
        created_at=100,
    )
    current = encode_week_event(
        current_aggregate,
        private_key_hex=PRIVATE_KEY,
        provider_d="provider",
        created_at=101,
    )
    await enqueue_signed_event(session_factory, closed, stored_at_ms=3)
    await enqueue_signed_event(session_factory, current, stored_at_ms=4)
    attempts: list[str] = []

    async def sender(
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        attempts.append(event_id)
        return RelaySendResult(True, True)

    delivery = AnalyticsV2Delivery(
        session_factory,
        operator_relays=RELAYS,
        sender=sender,
        relay_limit_reader=_no_advertised_limit,
    )
    assert await delivery.deliver_pending_once(at_ms=5) == (
        delivery_module.DeliveryPassResult(2, 2)
    )
    assert attempts == [closed.event_id] * 3 + [current.event_id] * 3
    async with session_factory() as session:
        rows = (
            await session.exec(
                select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
            )
        ).all()
    assert [(row.epoch, row.status) for row in rows] == [
        (0, "delivered"),
        (1, "delivered"),
    ]


@pytest.mark.asyncio
async def test_open_versions_coalesce_but_finalized_versions_are_retained(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, second = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    await enqueue_signed_event(session_factory, second, stored_at_ms=4)

    final_aggregate = aggregate_ledger_week(
        [],
        epoch=0,
        epoch_coverage_start=WEEK,
        epoch_coverage_end=None,
        week=WEEK,
        today_utc=WEEK + timedelta(days=7),
        prior_version=second.as_prior_version(),
    )
    assert final_aggregate is not None
    final = encode_week_event(
        final_aggregate,
        private_key_hex=PRIVATE_KEY,
        provider_d="provider",
        created_at=102,
    )
    await enqueue_signed_event(session_factory, final, stored_at_ms=5)
    async with session_factory() as session:
        stored_final = await session.get(AnalyticsV2Outbox, final.event_id)
        assert stored_final is not None
        assert stored_final.status == "pending"
        stored_final.first_send_attempt_at_ms = 5
        await session.commit()

    late = LedgerOutcome(
        terminal_day=WEEK,
        model_identifier="model/a",
        input_source="reported",
        output_source="reported",
        cache_read_source="missing",
        cache_creation_source="missing",
        input_tokens=1,
        output_tokens=1,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=1,
    )
    correction_aggregate = aggregate_ledger_week(
        [late],
        epoch=0,
        epoch_coverage_start=WEEK,
        epoch_coverage_end=None,
        week=WEEK,
        today_utc=WEEK + timedelta(days=7),
        prior_version=final.as_prior_version(),
        correction=True,
    )
    assert correction_aggregate is not None
    correction = encode_week_event(
        correction_aggregate,
        private_key_hex=PRIVATE_KEY,
        provider_d="provider",
        created_at=103,
    )
    inserted = await enqueue_signed_event(session_factory, correction, stored_at_ms=6)
    repeated = await enqueue_signed_event(session_factory, correction, stored_at_ms=7)
    assert inserted.inserted is True
    assert repeated.inserted is False

    async with session_factory() as session:
        result = await session.exec(
            select(AnalyticsV2Outbox).order_by(col(AnalyticsV2Outbox.created_at))
        )
        rows = result.all()
    # Only its own correction retires a finalized report that may be on relays.
    assert [(row.status, row.finalized) for row in rows] == [
        ("superseded", False),
        ("superseded", False),
        ("superseded", True),
        ("pending", True),
    ]
    assert bytes(rows[2].frame) == final.frame


@pytest.mark.asyncio
async def test_duplicate_event_id_requires_exact_outbox_readback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    async with session_factory() as session:
        stored = await session.get(AnalyticsV2Outbox, first.event_id)
        assert stored is not None
        stored.d_tag = "conflicting-coordinate"
        await session.commit()

    with pytest.raises(OutboxConflictError, match="conflicting outbox"):
        await enqueue_signed_event(session_factory, first, stored_at_ms=4)


@pytest.mark.asyncio
async def test_disable_cancels_inflight_and_old_generation_never_retries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_sender(
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        started.set()
        try:
            await asyncio.Future()
            raise AssertionError("blocking sender unexpectedly resumed")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    delivery = AnalyticsV2Delivery(
        session_factory,
        operator_relays=RELAYS,
        sender=blocking_sender,
        relay_limit_reader=_no_advertised_limit,
    )
    delivery_pass = asyncio.create_task(delivery.deliver_pending_once(at_ms=4))
    await started.wait()
    disabled = await delivery.disable(at_ms=_at_ms(WEEK + timedelta(days=2)))
    await delivery_pass

    assert cancelled.is_set()
    assert disabled.sharing_enabled is False
    assert disabled.generation == 2
    assert await delivery.resume() is False
    assert await delivery.deliver_pending_once(at_ms=6) == (
        delivery_module.DeliveryPassResult(0, 0)
    )
    async with session_factory() as session:
        row = await session.get(AnalyticsV2Outbox, first.event_id)
    assert row is not None and row.status == "cancelled"


@pytest.mark.asyncio
async def test_disable_during_receipt_lookup_prevents_sender_start(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    lookup_started = asyncio.Event()
    resume_lookup = asyncio.Event()
    sends = 0
    original_exec = AsyncSession.exec

    async def paused_exec(
        session: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if "analytics_v2_relay_receipts" in str(statement):
            lookup_started.set()
            await resume_lookup.wait()
        return await original_exec(session, statement, *args, **kwargs)

    async def sender(
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        nonlocal sends
        sends += 1
        return RelaySendResult(True, True)

    monkeypatch.setattr(AsyncSession, "exec", paused_exec)
    delivery = AnalyticsV2Delivery(
        session_factory,
        operator_relays=RELAYS,
        sender=sender,
        relay_limit_reader=_no_advertised_limit,
    )
    delivery_pass = asyncio.create_task(delivery.deliver_pending_once(at_ms=4))
    await lookup_started.wait()
    await delivery.disable(at_ms=_at_ms(WEEK + timedelta(days=2)))
    resume_lookup.set()

    assert await delivery_pass == delivery_module.DeliveryPassResult(0, 0)
    assert sends == 0


@pytest.mark.asyncio
async def test_cross_instance_disable_fences_event_write_after_handshake(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    handshake_started = asyncio.Event()
    resume_handshake = asyncio.Event()
    event_writes = 0

    class PausedWebSocket:
        MAX_REDIRECTS_ALLOWED = 10

        async def __aenter__(self) -> PausedWebSocket:
            handshake_started.set()
            await resume_handshake.wait()
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def send(self, message: str) -> None:
            nonlocal event_writes
            if json.loads(message)[0] == "EVENT":
                event_writes += 1

    async def resolved(url: str) -> ResolvedRelayEndpoint:
        return ResolvedRelayEndpoint("8.8.8.8", 443, "relay.valid.net")

    monkeypatch.setattr(delivery_module, "resolve_public_relay_endpoint", resolved)
    monkeypatch.setattr(
        delivery_module.websockets,
        "connect",
        lambda *args, **kwargs: PausedWebSocket(),
    )
    disabling_instance = AnalyticsV2Delivery(session_factory, operator_relays=RELAYS)
    sending_instance = AnalyticsV2Delivery(
        session_factory,
        operator_relays=RELAYS,
        relay_limit_reader=_no_advertised_limit,
    )
    delivery_pass = asyncio.create_task(sending_instance.deliver_pending_once(at_ms=4))
    await handshake_started.wait()
    disabled = await disabling_instance.disable(at_ms=_at_ms(WEEK + timedelta(days=2)))
    resume_handshake.set()

    assert await delivery_pass == delivery_module.DeliveryPassResult(0, 0)
    assert disabled.sharing_enabled is False
    assert event_writes == 0


@pytest.mark.asyncio
async def test_identity_rotation_is_atomic_and_cancels_pending_reports(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    rotated = await rotate_analytics_v2_identity(
        session_factory,
        pubkey=OTHER_PUBLIC_KEY,
        provider_d="other-provider",
        at_ms=4,
    )
    assert rotated.sharing_enabled is False
    assert rotated.generation == 2
    assert rotated.identity_pubkey == OTHER_PUBLIC_KEY
    assert rotated.provider_d == "other-provider"
    async with session_factory() as session:
        row = await session.get(AnalyticsV2Outbox, first.event_id)
    assert row is not None and row.status == "cancelled"
    assert await get_analytics_v2_delivery_state(session_factory) == rotated


@pytest.mark.asyncio
async def test_sdk_signature_randomness_reuses_the_first_durable_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    second, _ = _open_versions()
    assert first.event_id == second.event_id
    initial = await enqueue_signed_event(session_factory, first, stored_at_ms=3)
    duplicate = await enqueue_signed_event(session_factory, second, stored_at_ms=4)
    assert initial.inserted is True
    assert duplicate.inserted is False
    async with session_factory() as session:
        row = await session.get(AnalyticsV2Outbox, first.event_id)
    assert row is not None
    assert bytes(row.frame) == first.frame


@pytest.mark.asyncio
async def test_single_configured_relay_can_deliver_without_a_manifest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _activate(session_factory)
    first, _ = _open_versions()
    await enqueue_signed_event(session_factory, first, stored_at_ms=3)

    async def sender(
        target: RelayTarget,
        event_id: str,
        frame: bytes,
        is_active: Callable[[], Awaitable[bool]],
    ) -> RelaySendResult:
        assert await is_active()
        return RelaySendResult(True, True)

    delivery = AnalyticsV2Delivery(
        session_factory,
        operator_relays=(RELAYS[0], RELAYS[0]),
        sender=sender,
        relay_limit_reader=_no_advertised_limit,
    )
    assert await delivery.deliver_pending_once(
        at_ms=4
    ) == delivery_module.DeliveryPassResult(1, 1)


@pytest.mark.asyncio
async def test_frame_limit_uses_the_required_relay_quorum_and_caches_each_pass(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    reads: list[str] = []
    advertised = {RELAYS[0]: 12_000, RELAYS[1]: 8_000, RELAYS[2]: 500}

    async def reader(target: RelayTarget, timeout: float) -> int:
        reads.append(target.url)
        return advertised[target.url]

    delivery = AnalyticsV2Delivery(
        session_factory, operator_relays=RELAYS, relay_limit_reader=reader
    )
    assert await delivery.frame_limit() == 8_000
    assert set(
        await delivery._eligible_targets(delivery._targets, b"x" * 8_000)
    ) == set(delivery._targets[:2])
    assert sorted(reads) == sorted(RELAYS)


@pytest.mark.asyncio
async def test_unavailable_relay_information_keeps_a_bounded_default_frame_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def reader(target: RelayTarget, timeout: float) -> int:
        raise OSError("information endpoint unavailable")

    delivery = AnalyticsV2Delivery(
        session_factory, operator_relays=RELAYS, relay_limit_reader=reader
    )
    assert await delivery.frame_limit() == 96 * 1024
