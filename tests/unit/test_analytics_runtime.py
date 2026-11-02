from __future__ import annotations

import asyncio
import importlib
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel, col, select, text
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core import admin, terminal_outcomes, vault
from routstr.core.db import Secret, TerminalOutcomeEpoch
from routstr.core.main import app
from routstr.core.settings import Settings, SettingsService
from routstr.nostr import analytics_runtime as runtime
from routstr.nostr import listing


@pytest_asyncio.fixture
async def node(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
        # No saved row: each test drives the in-process flags directly.
        await connection.exec_driver_sql(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    events: list[str] = []

    async def publisher(*args: object) -> None:
        events.append("start:daily")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stop:daily")

    writer = terminal_outcomes.TerminalOutcomeWriter(session_factory=session_factory)
    monkeypatch.setattr(terminal_outcomes, "terminal_outcome_writer", writer)
    monkeypatch.setattr(runtime, "terminal_outcome_writer", writer)
    monkeypatch.setattr(runtime, "create_session", session_factory)
    monkeypatch.setattr(runtime, "run_analytics_v2_publisher", publisher)
    for key, value in {
        "nsec": "11" * 32,
        "provider_id": "stats-test-node",
        "relays": ["wss://relay.example.com"],
        "enable_analytics_sharing": False,
    }.items():
        monkeypatch.setattr(runtime.settings, key, value)
    coordinator = runtime.AnalyticsCoordinator()
    try:
        yield SimpleNamespace(
            coordinator=coordinator,
            writer=writer,
            sessions=session_factory,
            events=events,
        )
    finally:
        await coordinator.close(serving_stopped=True)
        await writer.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_collection_is_private_without_identity_or_sharing(
    node: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime.settings, "nsec", "")
    await node.coordinator.prepare_startup()
    await node.coordinator.sync_once()
    assert node.writer.running
    assert node.coordinator._task is None
    state = await runtime.get_analytics_v2_delivery_state(node.sessions)
    assert not state.sharing_enabled
    assert state.identity_pubkey is None


@pytest.mark.asyncio
async def test_public_opt_out_keeps_private_collection_running(
    node: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    await node.coordinator.prepare_startup()
    await node.coordinator.sync_once()
    await asyncio.sleep(0)
    assert node.coordinator._task is not None
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", False)
    await node.coordinator.sync_once()
    assert node.writer.running
    assert node.coordinator._task is None
    assert not (
        await runtime.get_analytics_v2_delivery_state(node.sessions)
    ).sharing_enabled
    assert node.events == ["start:daily", "stop:daily"]


@pytest.mark.asyncio
async def test_opt_out_saved_by_another_worker_is_not_undone(
    node: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    await node.coordinator.prepare_startup()
    await node.coordinator.sync_once()
    assert node.coordinator._task is not None

    # The other worker saved the opt-out and already disabled delivery.
    async with node.sessions() as session:
        await session.exec(  # type: ignore[call-overload]
            text("INSERT INTO settings (id, data) VALUES (1, :data)").bindparams(
                data=json.dumps({"enable_analytics_sharing": False})
            )
        )
        await session.commit()
    await runtime.transition_analytics_v2_sharing(node.sessions, enabled=False)

    await node.coordinator.sync_once()
    assert node.coordinator._task is None
    assert node.writer.running
    assert not (
        await runtime.get_analytics_v2_delivery_state(node.sessions)
    ).sharing_enabled


@pytest.mark.asyncio
async def test_relays_saved_by_another_worker_move_publication(
    node: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    await node.coordinator.prepare_startup()
    await node.coordinator.sync_once()
    assert node.coordinator._relays == ("wss://relay.example.com",)

    # Another worker saved new relays; this one still holds the old list.
    async with node.sessions() as session:
        await session.exec(  # type: ignore[call-overload]
            text("INSERT INTO settings (id, data) VALUES (1, :data)").bindparams(
                data=json.dumps(
                    {
                        "enable_analytics_sharing": True,
                        "relays": ["wss://relay.example.org"],
                    }
                )
            )
        )
        await session.commit()

    await node.coordinator.sync_once()
    await asyncio.sleep(0)
    assert node.coordinator._relays == ("wss://relay.example.org",)
    assert node.events == ["start:daily", "stop:daily", "start:daily"]


@pytest.mark.asyncio
async def test_missing_identity_does_not_stop_private_collection(
    node: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime.settings, "nsec", "")
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    await node.coordinator.prepare_startup()
    await node.coordinator.sync_once()
    assert node.writer.running
    assert node.coordinator._task is None
    assert not (
        await runtime.get_analytics_v2_delivery_state(node.sessions)
    ).sharing_enabled


@pytest.mark.asyncio
async def test_identity_rotation_cancels_previous_publisher(
    node: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    await node.coordinator.prepare_startup()
    await node.coordinator.sync_once()
    await asyncio.sleep(0)
    monkeypatch.setattr(runtime.settings, "nsec", "22" * 32)
    await node.coordinator.sync_once()
    await asyncio.sleep(0)
    state = await runtime.get_analytics_v2_delivery_state(node.sessions)
    keypair = runtime.nsec_to_keypair("22" * 32)
    assert keypair is not None
    assert state.identity_pubkey == keypair[1]
    assert node.events == ["start:daily", "stop:daily", "start:daily"]
    assert node.writer.running


@pytest.mark.asyncio
async def test_saved_identity_reaches_other_workers_without_disabling_sharing(
    node: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_module = importlib.import_module("routstr.core.settings")
    first = runtime.settings.copy(deep=True)
    first.nsec = ""
    first.npub = ""
    first.enable_analytics_sharing = True
    second = first.copy(deep=True)

    def bind_worker(settings: Settings) -> None:
        for module in (runtime, admin, listing, settings_module):
            monkeypatch.setattr(module, "settings", settings)
        monkeypatch.setattr(SettingsService, "_current", settings)

    async with node.sessions() as session:
        await session.exec(  # type: ignore[call-overload]
            text("INSERT INTO settings (id, data) VALUES (1, :data)").bindparams(
                data=json.dumps(settings_module._strip_secret_fields(first.dict()))
            )
        )
        await session.commit()
    monkeypatch.setattr(admin, "create_session", node.sessions)
    token = "stats-identity-test"
    monkeypatch.setitem(admin.admin_sessions, token, int(time.time()) + 60)
    other = runtime.AnalyticsCoordinator()
    try:
        for coordinator, settings in ((node.coordinator, first), (other, second)):
            bind_worker(settings)
            await coordinator.prepare_startup()
            await coordinator.sync_once()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            for key in ("11" * 32, "22" * 32, ""):
                bind_worker(first)
                response = await client.patch("/admin/api/nsec", json={"nsec": key})
                assert response.status_code == 200
                saved = await client.patch(
                    "/admin/api/settings", json={"npub": response.json()["npub"]}
                )
                assert saved.status_code == 200
                async with node.sessions() as session:
                    secret = await session.get(Secret, 1)
                    assert secret is not None
                    assert (
                        vault.decrypt(secret.encrypted_nsec)
                        if secret.encrypted_nsec
                        else ""
                    ) == key
                await node.coordinator.sync_once()
                state = await runtime.get_analytics_v2_delivery_state(node.sessions)
                for coordinator, settings in (
                    (other, second),
                    (node.coordinator, first),
                    (other, second),
                ):
                    bind_worker(settings)
                    await coordinator.sync_once()
                    current = await runtime.get_analytics_v2_delivery_state(
                        node.sessions
                    )
                    assert current.sharing_enabled is bool(key)
                    assert current.generation == state.generation
                    assert settings.nsec == key
                    assert settings.npub == response.json()["npub"]
    finally:
        await other.close(serving_stopped=True)


@pytest.mark.asyncio
async def test_restart_writer_failure_preserves_publication_before_serving(
    node: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    await node.coordinator.prepare_startup()
    await node.coordinator.sync_once()
    await node.coordinator.close(serving_stopped=True)
    assert (
        await runtime.get_analytics_v2_delivery_state(node.sessions)
    ).sharing_enabled

    before = await runtime.get_analytics_v2_delivery_state(node.sessions)

    async def failed_start() -> bool:
        return False

    monkeypatch.setattr(runtime, "start_terminal_outcome_writer", failed_start)
    restarted = runtime.AnalyticsCoordinator()
    await restarted.prepare_startup()
    await restarted.sync_once()
    after = await runtime.get_analytics_v2_delivery_state(node.sessions)
    assert after == before
    assert restarted._task is not None
    assert not node.writer.running
    await restarted.close(serving_stopped=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["discovery", "relay"])
async def test_identity_change_fences_siblings_even_if_setup_fails(
    node: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    await node.coordinator.prepare_startup()
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    await node.coordinator.sync_once()
    before = await runtime.get_analytics_v2_delivery_state(node.sessions)
    assert before.sharing_enabled
    monkeypatch.setattr(runtime.settings, "nsec", "22" * 32)
    if failure == "discovery":

        async def unavailable(*args: object) -> str:
            raise ValueError("provider discovery unavailable")

        monkeypatch.setattr(runtime, "resolve_provider_id_strict", unavailable)
    else:
        monkeypatch.setattr(runtime.settings, "relays", ["ws://public.example.com"])
    await node.coordinator.sync_once()
    after = await runtime.get_analytics_v2_delivery_state(node.sessions)
    assert not after.sharing_enabled
    assert after.generation == before.generation + 1
    assert node.writer.running


@pytest.mark.asyncio
async def test_saved_opt_out_survives_a_stale_worker_and_fences_activation(
    node: Any, monkeypatch: Any
) -> None:
    public = {"enable_analytics_sharing": True}
    async with node.sessions() as session:
        await SettingsService.initialize(session)
        await SettingsService.update(public, session)
    seen = (await runtime.get_analytics_v2_delivery_state(node.sessions)).generation

    # Another worker saves the opt-out; this one still holds the old flags.
    async with node.sessions() as session:
        row = (await session.exec(text("SELECT data FROM settings WHERE id = 1"))).one()  # type: ignore[call-overload]
        saved = {**json.loads(row[0]), "enable_analytics_sharing": False}
        await session.exec(  # type: ignore[call-overload]
            text("UPDATE settings SET data = :data WHERE id = 1").bindparams(
                data=json.dumps(saved)
            )
        )
        await session.commit()
    async with node.sessions() as session:
        await SettingsService.update({"name": "renamed"}, session)
        row = (await session.exec(text("SELECT data FROM settings WHERE id = 1"))).one()  # type: ignore[call-overload]
    assert json.loads(row[0])["enable_analytics_sharing"] is False
    assert not runtime.settings.enable_analytics_sharing

    # Saving the opt-out itself moves the fence in the same commit.
    async with node.sessions() as session:
        await SettingsService.update({"enable_analytics_sharing": False}, session)
    state = await runtime.get_analytics_v2_delivery_state(node.sessions)
    assert not state.sharing_enabled
    assert state.generation == seen + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sharing_enabled", [True, False])
async def test_upgrade_preserves_existing_sharing_choice_across_restart(
    node: Any, sharing_enabled: bool
) -> None:
    async with node.sessions() as session:
        await session.exec(  # type: ignore[call-overload]
            text("INSERT INTO settings (id, data) VALUES (1, :data)").bindparams(
                data=json.dumps(
                    {
                        "enable_analytics_sharing": sharing_enabled,
                        "provider_id": "stats-test-node",
                        "relays": ["wss://relay.example.com"],
                    }
                )
            )
        )
        await session.commit()
        restored = await SettingsService.initialize(session)
        assert restored.enable_analytics_sharing is sharing_enabled
    await node.coordinator.prepare_startup()
    await node.coordinator.sync_once()
    await asyncio.sleep(0)
    assert node.events == (["start:daily"] if sharing_enabled else [])
    assert node.writer.running
    state = await runtime.get_analytics_v2_delivery_state(node.sessions)
    assert state.sharing_enabled is sharing_enabled
    if sharing_enabled:
        async with node.sessions() as session:
            epoch = (
                await session.exec(
                    select(TerminalOutcomeEpoch).where(
                        col(TerminalOutcomeEpoch.current_slot) == 1
                    )
                )
            ).one()
        assert epoch.coverage_start_day == datetime.now(UTC).date() + timedelta(days=1)
    await node.coordinator.close(serving_stopped=True)

    async with node.sessions() as session:
        restored = await SettingsService.initialize(session)
        assert restored.enable_analytics_sharing is sharing_enabled
    restarted = runtime.AnalyticsCoordinator()
    try:
        await restarted.prepare_startup()
        await restarted.sync_once()
        await asyncio.sleep(0)
        assert node.writer.running
        state = await runtime.get_analytics_v2_delivery_state(node.sessions)
        assert state.sharing_enabled is sharing_enabled
        if not sharing_enabled:
            assert restarted._task is None
            assert node.events == []
    finally:
        await restarted.close(serving_stopped=True)


@pytest.mark.asyncio
async def test_relays_without_a_public_target_never_activate_sharing(
    node: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    monkeypatch.setattr(runtime.settings, "relays", ["ws://umbrel.local:4848"])
    await node.coordinator.prepare_startup()
    for _ in range(2):
        node.coordinator._retry_at = 0
        await node.coordinator.sync_once()

    state = await runtime.get_analytics_v2_delivery_state(node.sessions)
    assert (state.sharing_enabled, state.generation) == (False, 0)
    assert node.coordinator._task is None
    assert node.writer.running


@pytest.mark.asyncio
async def test_missing_provider_identity_warns_once(
    node: Any, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    async def unlisted(*args: object) -> str:
        raise ValueError(
            "Configure PROVIDER_ID to select one provider for public stats"
        )

    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    monkeypatch.setattr(runtime.settings, "provider_id", "")
    monkeypatch.setattr(runtime, "resolve_provider_id_strict", unlisted)
    runtime_logger = logging.getLogger(runtime.__name__)
    runtime_logger.addHandler(caplog.handler)
    try:
        await node.coordinator.prepare_startup()
        for _ in range(3):
            node.coordinator._retry_at = 0
            await node.coordinator.sync_once()
    finally:
        runtime_logger.removeHandler(caplog.handler)

    records = [r for r in caplog.records if r.name == runtime.__name__]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "PROVIDER_ID" in records[0].__dict__["reason"]
    assert node.coordinator._task is None


@pytest.mark.asyncio
async def test_coordinator_cancellation_does_not_stop_collection(
    node: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await node.coordinator.prepare_startup()
    entered = asyncio.Event()

    async def sync_once() -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(node.coordinator, "sync_once", sync_once)
    task = asyncio.create_task(node.coordinator.run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert node.writer.running
    assert not node.coordinator._closed
    assert node.coordinator._task is None
    assert await node.writer.flush(timeout=1)


@pytest.mark.asyncio
async def test_collection_shutdown_requires_explicit_drained_serving(node: Any) -> None:
    await node.coordinator.prepare_startup()
    with pytest.raises(RuntimeError, match="drain settlement"):
        await node.coordinator.close()
    assert node.writer.running
    assert not node.coordinator._closed
    await node.coordinator.close(serving_stopped=True)
    assert not node.writer.running


@pytest.mark.asyncio
async def test_worker_shutdown_does_not_stop_other_worker_collection(node: Any) -> None:
    from routstr.core.db import TerminalOutcomeWriterRun

    first = node.writer
    second = terminal_outcomes.TerminalOutcomeWriter(session_factory=node.sessions)
    assert await first.start()
    first_id = first._run_id
    assert await second.start()
    second_id = second._run_id
    assert await first.stop(timeout=1)
    assert second.running
    assert await second.flush(timeout=1)
    async with node.sessions() as session:
        first_run = await session.get(TerminalOutcomeWriterRun, first_id)
        second_run = await session.get(TerminalOutcomeWriterRun, second_id)
    assert first_run is not None and first_run.status == "clean"
    assert second_run is not None and second_run.status == "active"
    assert await second.stop(timeout=1)


@pytest.mark.asyncio
async def test_shutdown_drains_detached_settlement_before_writer_stop(
    node: Any,
) -> None:
    from routstr.upstream.stream_ownership import PersistentStreamFinalizer

    await node.coordinator.prepare_startup()
    entered, release = asyncio.Event(), asyncio.Event()

    async def settle() -> None:
        entered.set()
        await release.wait()
        assert node.writer.running

    finalizer = PersistentStreamFinalizer(settle)
    request = asyncio.create_task(finalizer.run())
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    closing = asyncio.create_task(node.coordinator.close(serving_stopped=True))
    await asyncio.sleep(0)
    assert not closing.done()
    assert node.writer.running
    release.set()
    await closing
    assert not node.writer.running


@pytest.mark.asyncio
async def test_shutdown_drains_settlement_when_collector_never_started(
    node: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routstr.upstream.stream_ownership import PersistentStreamFinalizer

    async def failed_start() -> bool:
        return False

    monkeypatch.setattr(runtime, "start_terminal_outcome_writer", failed_start)
    await node.coordinator.prepare_startup()
    settled = asyncio.Event()

    async def settle() -> None:
        settled.set()

    PersistentStreamFinalizer(settle)
    await node.coordinator.close(serving_stopped=True)
    assert settled.is_set()


@pytest.mark.asyncio
async def test_incomplete_settlement_is_not_closed_as_clean(node: Any) -> None:
    from routstr.core.db import TerminalOutcomeWriterRun
    from routstr.upstream.stream_ownership import PersistentStreamFinalizer

    await node.coordinator.prepare_startup()

    async def fail() -> None:
        raise RuntimeError("synthetic incomplete settlement")

    PersistentStreamFinalizer(fail)
    await node.coordinator.close(serving_stopped=True)
    async with node.sessions() as session:
        runs = (await session.exec(select(TerminalOutcomeWriterRun))).all()
    assert runs
    assert all(run.status != "clean" for run in runs)
    assert any(run.loss_reason == "continuity_lost" for run in runs)


@pytest.mark.asyncio
async def test_pending_finalizer_cannot_leave_clean_restart_proof(
    node: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routstr.core.db import TerminalOutcomeWriterRun
    from routstr.upstream import stream_ownership

    await node.coordinator.prepare_startup()
    monkeypatch.setattr(
        stream_ownership.settings, "request_cleanup_timeout_seconds", 0.01
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def stubborn_settlement() -> None:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    finalizer = stream_ownership.PersistentStreamFinalizer(stubborn_settlement)
    request = asyncio.create_task(finalizer.run())
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    try:
        await asyncio.wait_for(node.coordinator.close(serving_stopped=True), timeout=1)
        assert finalizer._task is not None and not finalizer._task.done()
        async with node.sessions() as session:
            runs = (await session.exec(select(TerminalOutcomeWriterRun))).all()
        assert runs
        assert all(run.status != "clean" for run in runs)
        # A later process must recover the unresolved run conservatively.
        now = int(time.time() * 1000) + 120_000
        restarted = terminal_outcomes.TerminalOutcomeWriter(
            session_factory=node.sessions, clock=lambda: now
        )
        try:
            assert await restarted.start()
            async with node.sessions() as session:
                recovered = (await session.exec(select(TerminalOutcomeWriterRun))).all()
            assert any(
                run.status == "recovered"
                and run.loss_day == datetime.now(UTC).date()
                and run.loss_reason == "continuity_lost"
                for run in recovered
            )
        finally:
            await restarted.stop()
    finally:
        release.set()
        assert finalizer._task is not None
        await finalizer._task


@pytest.mark.asyncio
async def test_request_cleanup_outliving_asgi_prevents_clean_stop(
    node: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routstr.core import lifecycle
    from routstr.core.db import TerminalOutcomeWriterRun

    await node.coordinator.prepare_startup()
    monkeypatch.setattr(lifecycle.settings, "request_cleanup_timeout_seconds", 0.01)
    release, entered = asyncio.Event(), asyncio.Event()

    async def application(scope: Any, receive: Any, send: Any) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

    async def receive() -> Any:
        await entered.wait()
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        pass

    monkeypatch.setattr(lifecycle.settings, "max_request_lifetime_seconds", 0.01)
    middleware = lifecycle.RequestLifecycleMiddleware(application)
    await middleware({"type": "http"}, receive, send)
    pending = lifecycle._request_work.copy()
    assert pending and all(not task.done() for task in pending)
    try:
        await asyncio.wait_for(node.coordinator.close(serving_stopped=True), timeout=1)
        async with node.sessions() as session:
            runs = (await session.exec(select(TerminalOutcomeWriterRun))).all()
        assert all(run.status != "clean" for run in runs)
    finally:
        release.set()
        await asyncio.gather(*pending)
