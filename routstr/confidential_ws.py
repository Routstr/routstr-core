"""Confidential-upstream websocket proxies (node side).

* ``GET /v1/confidential/ws?session_id=<uuid>&v=<n>``: the session control
  and TLS-record channel to the cu-sidecar. The first client frame must be
  ``setup`` (within ``SETUP_TIMEOUT_S``). It carries the client's ordinary
  bearer (``auth``: an ``sk-`` key or a Cashu token) and optionally the
  ``offer_sig`` of the signed offer the client verified. The node validates
  the setup, freezes the rates, reserves, and only then dials the sidecar and
  forwards the setup *without* ``auth``/``offer_sig`` (the non-secret
  reservation id binds the session id instead). Afterwards it intercepts:

  - ``ready``: announces the node's Nostr key (the sidecar's offer is dropped:
    the node-signed offer is the only one);
  - ``body_released``: the upstream now has the complete request;
  - ``usage``: the π_C3-verified usage (or upstream error status) for this
    session's sid; the node settles it and returns a signed receipt;
  - ``attestation``: re-signed with the node's key.

  When the session ends without settlement, the reservation is released if
  the upstream never received the complete request, and charged in full
  otherwise (the client withheld its usage disclosure). Every refusal or
  failure is reported as ``{"type": "error", "status", "reason", "detail"}``
  before the socket closes.

* ``GET /v1/confidential/zk?proof=<pi_*>&session_id=<sid>``: a byte proxy for
  one ZK proof.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any, TypeGuard

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .confidential import (
    CONFIDENTIAL_MAX_V,
    CONFIDENTIAL_MIN_V,
    UNBILLED_ERROR_STATUSES,
    Billing,
    BillingError,
    cached_sidecar_offer,
    canonical_json,
    charge_reservation,
    node_pubkey_hex,
    release,
    reserve,
    settle,
    sign_payload,
    to_ws_url,
)
from .core import get_logger
from .core.settings import settings

logger = get_logger(__name__)

confidential_ws_router = APIRouter()

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# The zk route is keyed by the protocol sid (a 32-byte hex digest).
_SID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_PROOF_RE = re.compile(r"^(pi_c1|pi_n|pi_c2|pi_c3)$")
_WS_MAX_SIZE = 64 << 20

# Limits: concurrent sessions per process, time to the setup frame, a whole
# session's lifetime, and the largest request body a setup may declare.
MAX_SESSIONS = 32
SETUP_TIMEOUT_S = 15.0
SESSION_TIMEOUT_S = 600.0
MAX_BODY_LEN = 8 << 20

_active_sessions = 0


def _sidecar_ws_url(path: str, query: str) -> str:
    base = to_ws_url(settings.confidential_sidecar_url.rstrip("/"))
    return f"{base}{path}?{query}"


def _short(sid: str | None) -> str:
    return (sid or "")[:12]


class SessionState:
    def __init__(self) -> None:
        self.billing: Billing | None = None
        self.sid: str | None = None
        self.body_released = False
        # Set once the reservation is charged or released; ``lock`` makes
        # settlement and abort handling finalize it exactly once.
        self.finalized = False
        self.lock = asyncio.Lock()
        self.attestation_hash = ""


class SetupError(Exception):
    def __init__(self, status: int, reason: str, detail: Any = None) -> None:
        super().__init__(reason)
        self.status = status
        self.detail = detail if detail is not None else reason


async def _send_error(
    websocket: WebSocket, status: int, reason: str, detail: Any = None
) -> None:
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "error",
                    "status": status,
                    "reason": reason,
                    "detail": detail if detail is not None else reason,
                }
            )
        )
    except Exception:
        pass


async def _close(ws: Any, code: int = 1008, reason: str = "") -> None:
    try:
        await ws.close(code=code, reason=reason[:120])
    except Exception:
        pass


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_setup(msg: Any, max_tokens_cap: int) -> None:
    """Shape and range checks on a client ``setup`` before anything is reserved."""
    if not isinstance(msg, dict) or msg.get("type") != "setup":
        raise SetupError(400, "the first frame must be setup")
    model = msg.get("model")
    if not isinstance(model, str) or not model:
        raise SetupError(400, "setup.model must be a non-empty string")
    max_tokens = msg.get("max_tokens")
    if not _is_int(max_tokens) or not 1 <= max_tokens <= max_tokens_cap:
        raise SetupError(
            400, f"setup.max_tokens must be an integer in [1, {max_tokens_cap}]"
        )
    length = msg.get("len")
    if not _is_int(length) or not 1 <= length <= MAX_BODY_LEN:
        raise SetupError(400, f"setup.len must be an integer in [1, {MAX_BODY_LEN}]")
    nonce_c = msg.get("nonce_c")
    if not isinstance(nonce_c, str) or not _SID_RE.match(nonce_c):
        raise SetupError(400, "setup.nonce_c must be 64 hex characters")
    offer_sig = msg.get("offer_sig")
    if offer_sig is not None and (
        not isinstance(offer_sig, str) or not _HEX_RE.match(offer_sig)
    ):
        raise SetupError(400, "setup.offer_sig must be a hex string")
    if not isinstance(msg.get("auth", ""), str):
        raise SetupError(400, "setup.auth must be a string")


# ---------------------------------------------------------------------------
# /ws
# ---------------------------------------------------------------------------


@confidential_ws_router.websocket("/v1/confidential/ws")
async def confidential_session_proxy(
    websocket: WebSocket, session_id: str = "", v: str = ""
) -> None:
    global _active_sessions
    await websocket.accept()
    try:
        version = int(v or CONFIDENTIAL_MIN_V)
    except ValueError:
        version = -1
    if not _UUID_RE.match(session_id or ""):
        await websocket.close(code=1008, reason="invalid session_id")
        return
    if not CONFIDENTIAL_MIN_V <= version <= CONFIDENTIAL_MAX_V:
        await websocket.close(code=1008, reason=f"unsupported protocol version {v}")
        return
    if not node_pubkey_hex():
        await websocket.close(code=1011, reason="node has no signing identity")
        return
    if _active_sessions >= MAX_SESSIONS:
        await _send_error(websocket, 503, "too many confidential sessions")
        await _close(websocket, 1013, "too many confidential sessions")
        return
    _active_sessions += 1
    try:
        await _run_session(websocket, session_id)
    finally:
        _active_sessions -= 1


async def _run_session(websocket: WebSocket, session_id: str) -> None:
    started = time.monotonic()
    state = SessionState()
    upstream = await _open_session(websocket, session_id, state)
    if upstream is None:
        return

    client_pump = asyncio.create_task(_client_to_sidecar(websocket, upstream, state))
    sidecar_pump = asyncio.create_task(_sidecar_to_client(websocket, upstream, state))
    try:
        remaining = SESSION_TIMEOUT_S - (time.monotonic() - started)
        done, _ = await asyncio.wait(
            {client_pump, sidecar_pump},
            timeout=max(remaining, 0.0),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            logger.warning(
                "confidential ws: session time limit reached",
                extra={"sid": _short(state.sid)},
            )
            await _send_error(websocket, 408, "session time limit reached")
        else:
            # If the client left, the sidecar session is closing: let its pump
            # drain frames already in flight (body_released, usage) so the
            # money decision below sees the sidecar's real final state.
            remaining = SESSION_TIMEOUT_S - (time.monotonic() - started)
            await asyncio.wait({sidecar_pump}, timeout=min(10.0, max(remaining, 0)))
    finally:
        for task in (client_pump, sidecar_pump):
            if not task.done():
                task.cancel()
        await _finalize(state)
        await _close(upstream, 1000)
        await _close(websocket, 1000)


async def _open_session(
    websocket: WebSocket, session_id: str, state: SessionState
) -> Any:
    """Wait for ``setup``, validate it, reserve, then dial the sidecar.

    Returns the sidecar connection, or ``None`` after reporting an error.
    """
    try:
        try:
            message = await asyncio.wait_for(websocket.receive(), SETUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise SetupError(408, "no setup frame received in time") from None
        if message["type"] == "websocket.disconnect":
            return None
        try:
            msg = json.loads(message.get("text") or "")
        except json.JSONDecodeError:
            msg = None
        try:
            side = await cached_sidecar_offer()
            cap = int(side.get("max_tokens_cap", 4096))
        except Exception as exc:
            logger.warning("confidential ws: sidecar offer unavailable: %s", exc)
            raise SetupError(503, "confidential sidecar unavailable") from exc
        validate_setup(msg, cap)
        auth = msg.pop("auth", "") or ""
        offer_sig = msg.pop("offer_sig", None)
        try:
            state.billing = await reserve(
                auth, msg["model"], msg["max_tokens"], offer_sig
            )
        except BillingError as exc:
            # Same status codes as the normal proxy (e.g. 402 insufficient
            # balance), so the client's existing top-up/retry handling applies.
            raise SetupError(exc.status, str(exc), exc.detail) from exc
        except Exception as exc:
            logger.exception("confidential ws: reservation failed")
            raise SetupError(500, "reservation failed") from exc
        # The sidecar binds sid to this value; it is unique and not a secret.
        msg["escrow"] = state.billing.snapshot.release_id
        try:
            upstream = await websockets.connect(
                _sidecar_ws_url("/session", f"session_id={session_id}"),
                max_size=_WS_MAX_SIZE,
                open_timeout=10,
            )
        except Exception as exc:
            raise SetupError(502, "confidential sidecar unreachable") from exc
        try:
            await upstream.send(json.dumps(msg))
        except Exception as exc:
            await _close(upstream, 1011)
            raise SetupError(502, "confidential sidecar unreachable") from exc
        return upstream
    except (WebSocketDisconnect, RuntimeError):
        await _finalize(state)
        return None
    except SetupError as exc:
        logger.warning("confidential ws: rejecting session: %s", exc)
        await _finalize(state)
        await _send_error(websocket, exc.status, str(exc), exc.detail)
        await _close(websocket, 1008, str(exc))
        return None


async def _client_to_sidecar(
    websocket: WebSocket, upstream: Any, state: SessionState
) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
                continue
            text = message.get("text")
            if text is None:
                continue
            if _is_setup(text):
                await _send_error(websocket, 400, "duplicate setup")
                await _close(websocket, 1008, "duplicate setup")
                break
            await upstream.send(text)
    except (WebSocketDisconnect, RuntimeError, websockets.ConnectionClosed):
        pass
    finally:
        await _close(upstream, 1000)


def _is_setup(text: str) -> bool:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(msg, dict) and msg.get("type") == "setup"


async def _sidecar_to_client(
    websocket: WebSocket, upstream: Any, state: SessionState
) -> None:
    try:
        async for frame in upstream:
            if isinstance(frame, bytes):
                await websocket.send_bytes(frame)
                continue
            outgoing = await _on_sidecar_text(frame, websocket, state)
            if outgoing is not None:
                await websocket.send_text(outgoing)
    except (websockets.ConnectionClosed, RuntimeError):
        pass
    finally:
        await _close(websocket, 1000)


async def _on_sidecar_text(
    frame: str, websocket: WebSocket, state: SessionState
) -> str | None:
    try:
        msg = json.loads(frame)
    except json.JSONDecodeError:
        return frame
    kind = msg.get("type") if isinstance(msg, dict) else None
    if kind == "ready":
        state.sid = msg.get("sid")
        msg.pop("offer", None)
        msg["pubkey"] = node_pubkey_hex()
        msg["sig_scheme"] = "schnorr-secp256k1"
        return json.dumps(msg)
    if kind == "body_released":
        state.body_released = True
        return frame
    if kind == "attestation" and msg.get("a") is not None:
        a_json = canonical_json(msg["a"])
        state.attestation_hash = hashlib.sha256(a_json).hexdigest()
        msg["a_json"] = a_json.decode("utf-8")
        msg["sidecar_sig"] = msg.get("sig")
        msg["sig"] = sign_payload(msg["a"])
        msg["sig_scheme"] = "schnorr-secp256k1"
        return json.dumps(msg)
    if kind == "usage":
        if state.sid is None or msg.get("sid") != state.sid:
            logger.warning(
                "confidential ws: ignoring usage for another sid",
                extra={"sid": _short(state.sid)},
            )
            return None
        # Shielded: a client disconnect must not cancel settlement halfway.
        await asyncio.shield(_settle(msg, websocket, state))
        return None
    return frame


async def _apply_settlement(
    billing: Billing, usage: dict[str, Any]
) -> tuple[int, int | None]:
    """Finalize the reservation for a disclosed usage event: (cost, balance)."""
    error_status = usage.get("error_status")
    if _is_int(error_status):
        if error_status in UNBILLED_ERROR_STATUSES:
            return 0, await release(billing)
        # 500/502/504 …: work may have been billed upstream.
        return billing.reserved_msats, await charge_reservation(billing)
    if usage.get("model") != billing.model_id:
        # The pinned suffix fixes the model; anything else is a protocol breach.
        return billing.reserved_msats, await charge_reservation(billing)
    settled = await settle(billing, usage)
    return int(settled["cost_msats"]), settled["balance_msats"]


async def _settle(
    msg: dict[str, Any], websocket: WebSocket, state: SessionState
) -> None:
    billing = state.billing
    if billing is None:
        return
    raw = msg.get("usage")
    usage: dict[str, Any] = raw if isinstance(raw, dict) else {}
    async with state.lock:
        if state.finalized:
            return
        try:
            cost, balance = await _apply_settlement(billing, usage)
        except Exception:
            logger.exception(
                "confidential ws: settlement failed",
                extra={"sid": _short(state.sid), "model": billing.model_id},
            )
            outcome = await _finalize_unsettled(state)
            await _send_error(
                websocket,
                500,
                "settlement failed",
                {"error": {"type": "settlement_failed", "outcome": outcome}},
            )
            return
        state.finalized = True
    receipt = {
        "v": 1,
        "sid": state.sid,
        "model": billing.model_id,
        "usage": usage,
        "cost_msats": cost,
        "reserved_msats": billing.reserved_msats,
        "balance_msats": balance,
        "rates": billing.rates,
        "notary_pubkey": node_pubkey_hex(),
        "attestation_hash": state.attestation_hash,
        "time": int(time.time()),
    }
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "receipt",
                    "receipt": receipt,
                    "receipt_json": canonical_json(receipt).decode("utf-8"),
                    "sig": sign_payload(receipt),
                    "sig_scheme": "schnorr-secp256k1",
                }
            )
        )
    except Exception:
        pass
    logger.info(
        "confidential ws: settled",
        extra={"sid": _short(state.sid), "model": billing.model_id, "cost_msats": cost},
    )


async def _finalize_unsettled(state: SessionState) -> str | None:
    """Charge (body released) or release the reservation; caller holds the lock."""
    billing = state.billing
    if billing is None or state.finalized:
        return None
    try:
        if state.body_released:
            # The upstream got the complete request but no usage was settled.
            await charge_reservation(billing)
            outcome = "charged_reservation"
        else:
            # π_C2 never verified: the upstream never received a complete request.
            await release(billing)
            outcome = "released"
    except Exception:
        logger.exception(
            "confidential ws: could not finalize the reservation",
            extra={"sid": _short(state.sid)},
        )
        return None
    state.finalized = True
    return outcome


async def _finalize(state: SessionState) -> None:
    """A session that ended without settlement."""
    if state.billing is None:
        return
    async with state.lock:
        outcome = await _finalize_unsettled(state)
    if outcome is not None:
        logger.info(
            "confidential ws: session aborted",
            extra={"sid": _short(state.sid), "outcome": outcome},
        )


# ---------------------------------------------------------------------------
# /zk
# ---------------------------------------------------------------------------


@confidential_ws_router.websocket("/v1/confidential/zk")
async def confidential_zk_proxy(
    websocket: WebSocket, proof: str = "", session_id: str = ""
) -> None:
    await websocket.accept()
    if not _SID_RE.match(session_id or "") or not _PROOF_RE.match(proof or ""):
        await websocket.close(code=1008, reason="invalid session_id or proof")
        return
    try:
        upstream = await websockets.connect(
            _sidecar_ws_url("/zk", f"proof={proof}&sid={session_id}"),
            max_size=_WS_MAX_SIZE,
            open_timeout=10,
        )
    except Exception:
        await websocket.close(code=1011, reason="sidecar unreachable")
        return

    async def client_to_sidecar() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                await upstream.send(
                    data if data is not None else message.get("text", "")
                )
        except (WebSocketDisconnect, RuntimeError, websockets.ConnectionClosed):
            pass
        finally:
            await _close(upstream, 1000)

    async def sidecar_to_client() -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, bytes):
                    await websocket.send_bytes(frame)
                else:
                    await websocket.send_text(frame)
        except (websockets.ConnectionClosed, RuntimeError):
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    # Both directions must finish: the proof protocol's final flush would be
    # lost if one side were cancelled on the other's completion.
    tasks = {
        asyncio.create_task(client_to_sidecar()),
        asyncio.create_task(sidecar_to_client()),
    }
    await asyncio.wait(tasks, timeout=300)
    for task in tasks:
        if not task.done():
            task.cancel()
    try:
        await upstream.close()
    except Exception:
        pass


__all__ = ["confidential_ws_router"]
