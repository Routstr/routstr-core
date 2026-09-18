from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from ..core.db import create_session
from ..core.logging import get_logger
from ..core.settings import SettingsService, settings
from ..core.terminal_outcomes import (
    start_terminal_outcome_writer,
    stop_terminal_outcome_writer,
    terminal_outcome_writer,
)
from .analytics_v2_delivery import (
    AnalyticsV2Delivery,
    AnalyticsV2Producer,
    DeliveryStateSnapshot,
    SharingDisabledError,
    activate_analytics_v2_sharing,
    claim_analytics_v2_identity,
    get_analytics_v2_delivery_state,
    rotate_analytics_v2_identity,
    run_analytics_v2_publisher,
    transition_analytics_v2_sharing,
)
from .listing import DEFAULT_RELAY_URLS, nsec_to_keypair, resolve_provider_id_strict

logger = get_logger(__name__)


async def _read_state() -> DeliveryStateSnapshot:
    # Read the fence before the setting so a later opt-out refuses activation.
    state = await get_analytics_v2_delivery_state(create_session)
    async with create_session() as session:
        await SettingsService.refresh(session, ("enable_analytics_sharing",))
    return state


class AnalyticsCoordinator:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._delivery: AnalyticsV2Delivery | None = None
        self._identity: tuple[str, str, str] | None = None
        self._relays: tuple[str, ...] = ()
        self._writer_started = False
        self._retry_at = 0.0
        self._closed = False

    async def prepare_startup(self) -> None:
        state = await get_analytics_v2_delivery_state(create_session)
        self._writer_started = await start_terminal_outcome_writer()
        if state.sharing_enabled and (
            not settings.enable_analytics_sharing or not self._writer_started
        ):
            await transition_analytics_v2_sharing(create_session, enabled=False)

    async def run(self) -> None:
        try:
            while True:
                delay = 1
                try:
                    await self.sync_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Stats coordination failed; requests remain available"
                    )
                    delay = 10
                await asyncio.sleep(delay)
        finally:
            await self.close()

    async def sync_once(self) -> None:
        if self._closed:
            return
        state = await _read_state()
        wants_public = settings.enable_analytics_sharing
        if not wants_public:
            await self._stop_public(disable=state.sharing_enabled)
        if not terminal_outcome_writer.running:
            if time.monotonic() < self._retry_at:
                return
            self._writer_started = await start_terminal_outcome_writer(serving=True)
            if not self._writer_started:
                await self._stop_public(disable=state.sharing_enabled)
                self._retry_at = time.monotonic() + 10
                return

        if not wants_public:
            return

        if time.monotonic() < self._retry_at:
            return
        keypair = nsec_to_keypair(settings.nsec) if settings.nsec else None
        if keypair is None:
            await self._stop_public(disable=state.sharing_enabled)
            return
        private_key, pubkey = keypair
        relays = tuple(dict.fromkeys(settings.relays or DEFAULT_RELAY_URLS))
        if (
            state.identity_pubkey == pubkey
            and state.provider_d
            and (not settings.provider_id or settings.provider_id == state.provider_d)
        ):
            provider_d = state.provider_d
        else:
            try:
                provider_d = await resolve_provider_id_strict(pubkey, list(relays))
            except Exception:
                await self._stop_public(disable=state.sharing_enabled)
                self._retry_at = time.monotonic() + 60
                logger.exception("Stats need a stable provider identity before sharing")
                return
        identity = (private_key, pubkey, provider_d)
        if (
            self._identity == identity
            and self._relays == relays
            and state.sharing_enabled
            and self._task is not None
            and not self._task.done()
        ):
            return

        identity_changed = state.identity_pubkey is not None and (
            state.identity_pubkey != pubkey or state.provider_d != provider_d
        )
        await self._stop_public(disable=identity_changed)
        try:
            if identity_changed:
                await rotate_analytics_v2_identity(
                    create_session, pubkey=pubkey, provider_d=provider_d
                )
                state = await _read_state()
                if not settings.enable_analytics_sharing:
                    return
            else:
                claim = await claim_analytics_v2_identity(
                    create_session, pubkey=pubkey, provider_d=provider_d
                )
                if claim == "mismatch":
                    self._retry_at = time.monotonic() + 10
                    return
            await activate_analytics_v2_sharing(
                create_session,
                coverage_day=datetime.now(UTC).date(),
                expected_generation=state.generation,
            )
            producer = AnalyticsV2Producer(
                create_session,
                private_key_hex=private_key,
                public_key_hex=pubkey,
                provider_d=provider_d,
            )
            self._delivery = AnalyticsV2Delivery(
                create_session, operator_relays=list(relays)
            )
            self._identity = identity
            self._relays = relays
            self._task = asyncio.create_task(
                run_analytics_v2_publisher(producer, self._delivery),
                name="analytics-v2-publisher",
            )
        except SharingDisabledError:
            return
        except Exception:
            await self._stop_public(disable=True)
            self._retry_at = time.monotonic() + 60
            raise

    async def _stop_task(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Stats publisher stopped with an error")

    async def _stop_public(self, *, disable: bool) -> None:
        try:
            if disable:
                if self._delivery is not None:
                    await self._delivery.disable()
                else:
                    await transition_analytics_v2_sharing(create_session, enabled=False)
            elif self._delivery is not None:
                await self._delivery.stop()
        finally:
            await self._stop_task()
            self._delivery = None
            self._identity = None
            self._relays = ()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._stop_public(disable=False)
        finally:
            if self._writer_started:
                await stop_terminal_outcome_writer()
                self._writer_started = False


_coordinator: AnalyticsCoordinator | None = None


def _get_coordinator() -> AnalyticsCoordinator:
    global _coordinator
    if _coordinator is None or _coordinator._closed:
        _coordinator = AnalyticsCoordinator()
    return _coordinator


async def prepare_analytics() -> None:
    await _get_coordinator().prepare_startup()


async def run_analytics() -> None:
    await _get_coordinator().run()


async def shutdown_analytics() -> None:
    global _coordinator
    coordinator, _coordinator = _coordinator, None
    if coordinator is not None:
        await coordinator.close()
