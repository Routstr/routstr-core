from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from ..core.db import NsecState, Secret, create_session
from ..core.lifecycle import drain_request_work
from ..core.logging import get_logger
from ..core.settings import SettingsService, derive_npub_from_nsec, settings
from ..core.terminal_outcomes import (
    start_terminal_outcome_writer,
    stop_terminal_outcome_writer,
    terminal_outcome_writer,
)
from ..core.vault import decrypt
from ..upstream.stream_ownership import drain_stream_finalizers
from .analytics_v2_delivery import (
    AnalyticsV2Delivery,
    AnalyticsV2DeliveryError,
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


class AnalyticsCoordinator:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._delivery: AnalyticsV2Delivery | None = None
        self._identity: tuple[str, str, str] | None = None
        self._relays: tuple[str, ...] = ()
        self._writer_started = False
        self._retry_at = 0.0
        self._writer_retry_at = 0.0
        self._paused_reason: str | None = None
        self._closed = False
        self._secret_snapshot: tuple[NsecState, str | None] | None = None
        self._secret_identity: tuple[str, str] | None = None
        self._keypair_nsec: str | None = None
        self._keypair: tuple[str, str] | None = None

    async def _read_state(self) -> DeliveryStateSnapshot:
        # Always refresh the fence and settings; caching crypto must not delay
        # another worker's opt-out or identity rotation.
        state = await get_analytics_v2_delivery_state(create_session)
        async with create_session() as session:
            await SettingsService.refresh(
                session, ("enable_analytics_sharing", "relays", "provider_id")
            )
            secret = await session.get(Secret, 1)
            if secret is None or secret.nsec_state == NsecState.legacy:
                self._secret_snapshot = None
                self._secret_identity = None
            else:
                snapshot = (secret.nsec_state, secret.encrypted_nsec)
                if snapshot != self._secret_snapshot:
                    if secret.nsec_state == NsecState.encrypted:
                        if secret.encrypted_nsec is None:
                            raise RuntimeError(
                                "Encrypted Nostr identity has no ciphertext"
                            )
                        nsec = decrypt(secret.encrypted_nsec)
                    else:
                        nsec = ""
                    npub = derive_npub_from_nsec(nsec) or ""
                    # Never cache failed decryption/derivation attempts.
                    self._secret_identity = (nsec, npub)
                    self._secret_snapshot = snapshot
                if self._secret_identity is not None:
                    settings.nsec, settings.npub = self._secret_identity
        return state

    def _signing_keypair(self) -> tuple[str, str] | None:
        if settings.nsec != self._keypair_nsec:
            # Cache invalid/empty identities too, but only after parsing returns:
            # exceptions remain retryable on the next coordinator pass.
            keypair = nsec_to_keypair(settings.nsec) if settings.nsec else None
            self._keypair = keypair
            self._keypair_nsec = settings.nsec
        return self._keypair

    async def prepare_startup(self) -> None:
        state = await get_analytics_v2_delivery_state(create_session)
        self._writer_started = await start_terminal_outcome_writer()
        if state.sharing_enabled and not settings.enable_analytics_sharing:
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
            # The coordinator owns publication, not collection's lifetime.
            # Cancellation can happen while requests are still settling.
            await self._stop_public(disable=False)

    async def sync_once(self) -> None:
        if self._closed:
            return
        state = await self._read_state()
        wants_public = settings.enable_analytics_sharing
        if not wants_public:
            await self._stop_public(disable=state.sharing_enabled)
        if (
            not terminal_outcome_writer.running
            and time.monotonic() >= self._writer_retry_at
        ):
            self._writer_started = await start_terminal_outcome_writer()
            if not self._writer_started:
                self._writer_retry_at = time.monotonic() + 10

        if not wants_public:
            return

        if time.monotonic() < self._retry_at:
            return
        keypair = self._signing_keypair()
        if keypair is None:
            await self._stop_public(disable=state.sharing_enabled)
            return
        private_key, pubkey = keypair
        if state.identity_pubkey is not None and (
            state.identity_pubkey != pubkey
            or (settings.provider_id and settings.provider_id != state.provider_d)
        ):
            # Fence sibling senders before discovery/relay setup can fail.
            await self._stop_public(disable=state.sharing_enabled)
            state = await self._read_state()
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
            except ValueError as error:
                await self._pause(str(error), disable=False)
                return
        identity = (private_key, pubkey, provider_d)
        identity_changed = state.identity_pubkey is not None and (
            state.identity_pubkey != pubkey or state.provider_d != provider_d
        )
        if identity_changed:
            await self._stop_public(disable=state.sharing_enabled)
            state = await self._read_state()
        if (
            self._identity == identity
            and self._relays == relays
            and state.sharing_enabled
            and self._task is not None
            and not self._task.done()
        ):
            return
        try:
            delivery = AnalyticsV2Delivery(create_session, operator_relays=list(relays))
        except AnalyticsV2DeliveryError as error:
            # Checked before activation, so a node that cannot deliver never
            # opens and closes sharing on every retry.
            await self._pause(str(error), disable=False)
            return

        await self._stop_public(disable=False)
        try:
            if identity_changed:
                await rotate_analytics_v2_identity(
                    create_session, pubkey=pubkey, provider_d=provider_d
                )
                state = await self._read_state()
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
            self._delivery = delivery
            self._identity = identity
            self._relays = relays
            self._task = asyncio.create_task(
                run_analytics_v2_publisher(producer, delivery),
                name="analytics-v2-publisher",
            )
            self._paused_reason = None
        except SharingDisabledError:
            return
        except Exception:
            await self._stop_public(disable=False)
            self._retry_at = time.monotonic() + 60
            self._paused_reason = "Stats publisher failed to start; see node logs"
            raise

    async def _pause(self, reason: str, *, disable: bool) -> None:
        await self._stop_public(disable=disable)
        self._retry_at = time.monotonic() + 60
        # Retried every minute; warn again only when the cause changes.
        if reason != self._paused_reason:
            self._paused_reason = reason
            logger.warning("Stats sharing paused", extra={"reason": reason})

    def status(self) -> dict[str, object]:
        state: str
        reason: str | None = None
        if not settings.enable_analytics_sharing:
            state = "off"
        elif self._signing_keypair() is None:
            state = "no_identity"
            reason = "Set a Nostr NSEC to publish stats"
        elif self._task is not None and not self._task.done():
            state = "publishing"
        elif self._paused_reason is not None:
            state = "paused"
            reason = self._paused_reason
        else:
            state = "starting"
        retry_in = self._retry_at - time.monotonic()
        return {
            "state": state,
            "reason": reason,
            "provider_id": self._identity[2] if self._identity else None,
            "relays": list(self._relays),
            "retry_in_seconds": (
                int(retry_in) + 1 if state == "paused" and retry_in > 0 else None
            ),
        }

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

    async def close(self, *, serving_stopped: bool = False) -> None:
        if not serving_stopped:
            raise RuntimeError("Stop serving and drain settlement before collection")
        if self._closed:
            return
        self._closed = True
        try:
            await self._stop_public(disable=False)
        finally:
            self._secret_snapshot = None
            self._secret_identity = None
            self._keypair_nsec = None
            self._keypair = None
            # Billing settles even when the collector never started.
            requests_drained = await drain_request_work()
            settlement_drained = await drain_stream_finalizers() and requests_drained
            if not settlement_drained:
                terminal_outcome_writer.declare_loss(
                    "stream settlement did not drain before shutdown"
                )
            if self._writer_started:
                await stop_terminal_outcome_writer(allow_clean=settlement_drained)
                self._writer_started = False


_coordinator: AnalyticsCoordinator | None = None


def _get_coordinator() -> AnalyticsCoordinator:
    global _coordinator
    if _coordinator is None or _coordinator._closed:
        _coordinator = AnalyticsCoordinator()
    return _coordinator


def analytics_status() -> dict[str, object]:
    coordinator = _coordinator
    if coordinator is None or coordinator._closed:
        return {
            "state": "stopped",
            "reason": None,
            "provider_id": None,
            "relays": [],
            "retry_in_seconds": None,
        }
    return coordinator.status()


async def prepare_analytics() -> None:
    await _get_coordinator().prepare_startup()


async def run_analytics() -> None:
    await _get_coordinator().run()


async def shutdown_analytics(*, serving_stopped: bool = False) -> None:
    if not serving_stopped:
        raise RuntimeError("Analytics shutdown requires drained request settlement")
    global _coordinator
    coordinator = _coordinator
    if coordinator is not None:
        await coordinator.close(serving_stopped=serving_stopped)
        _coordinator = None
