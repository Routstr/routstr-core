from __future__ import annotations

import asyncio
import importlib
import json
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
        await coordinator.close()
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
        await other.close()


@pytest.mark.asyncio
async def test_restart_writer_failure_disables_publication_before_serving(
    node: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime.settings, "enable_analytics_sharing", True)
    await node.coordinator.prepare_startup()
    await node.coordinator.sync_once()
    await node.coordinator.close()
    assert (
        await runtime.get_analytics_v2_delivery_state(node.sessions)
    ).sharing_enabled

    async def failed_start() -> bool:
        return False

    monkeypatch.setattr(runtime, "start_terminal_outcome_writer", failed_start)
    restarted = runtime.AnalyticsCoordinator()
    await restarted.prepare_startup()
    assert not (
        await runtime.get_analytics_v2_delivery_state(node.sessions)
    ).sharing_enabled
    assert not node.writer.running
    await restarted.close()


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
        assert epoch.coverage_start_day == datetime.now(UTC).date() + timedelta(
            days=1
        )
    await node.coordinator.close()

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
        await restarted.close()
