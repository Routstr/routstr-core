"""Confidential-upstream websocket proxies (node side).

* ``GET /v1/confidential/ws?session_id=<uuid>&v=2``: the session control
  and TLS-record channel to the cu-sidecar. The first client frame must be
  ``setup`` (within ``SETUP_TIMEOUT_S``). It carries the client's ordinary
  bearer (``auth``: an ``sk-`` key or a Cashu token) and the ``offer_sig``
  of the signed offer the client verified. The node validates
  the setup, freezes the rates, reserves, and only then dials the sidecar and
  forwards the setup *without* ``auth``/``offer_sig`` (the non-secret
  reservation id binds the session id instead). Afterwards it intercepts:

  - ``ready``: announces the node's Nostr key (the sidecar's offer is dropped:
    the node-signed offer is the only one);
  - ``body_released``: the upstream now has the complete request;
  - ``usage``: the π_C3S-verified usage (or upstream error status) for this
    session's sid; the node settles it and returns a signed receipt;
  - ``attestation``: re-signed with the node's key.

  When the session ends without settlement, the reservation is released if
  the upstream never received the complete request, and charged in full
  otherwise (the client withheld its usage disclosure). With an abandon fee
  (``CONFIDENTIAL_ABANDON_FEE_MSAT``, or the session fee), a session the
  sidecar accepted (``ready``) is charged that fee instead of being released
  for free: the node did the 2PC work either way. Every refusal or failure is reported as
  ``{"type": "error", "status", "reason", "detail"}`` before the socket closes.

  An ``http: 2`` setup (protocol v3, option A; offered only when the sidecar
  offers it) opens one provider connection for many sequential requests. It
  names no model: the node checks the offer signature and that the
  credential has funds, reserves the offer's connection deposit (released
  when the connection's first request reaches settlement, charged when the
  connection ends without one unless the node, the sidecar or the provider
  failed; ``_h2_deposit_verdict``), then dials the sidecar. It closes a
  connection with no request in flight for the offer's ``idle_timeout_s``.
  Each ``request``
  frame then carries what a v2 setup carries (``auth``, ``offer_sig``, the
  fees, model, ``max_tokens``); the node validates it, freezes the rates and
  reserves as for a v2 setup, and forwards it without the credential. That
  request's ``body_released``, ``attestation`` and ``usage`` are handled as
  above, the receipt names the request (``n``), and the connection is idle
  again. A connection that ends between requests costs nothing more; one that ends
  during a request finalizes that request's reservation by the rules above.
  A request the node refuses ends the connection.

* ``GET /v1/confidential/zk?proof=<pi_*>&session_id=<sid>&zk_token=<token>``: a byte proxy for
  one ZK proof (``twopc``: the two-party handshake; ``zkchan``: the
  session's long-lived proof channel, which carries π_NK, π_WIN and π_C3S
  over correlations set up while the session is armed). Only the sid of a
  live session on this process (announced by the sidecar's ``ready``), with
  the token the node added to that ``ready``, is admitted, so the node must run as a single worker process.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import time
from typing import Any, TypeGuard

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .confidential import (
    FRAMINGS,
    IMAGE_MAX_COUNT,
    PROVIDER_REFUSAL_STATUSES,
    UNBILLED_ERROR_STATUSES,
    UPGRADE_REQUIRED,
    Billing,
    BillingError,
    cached_sidecar_offer,
    canonical_json,
    charge_fee,
    charge_reservation,
    check_credential,
    check_request_offer,
    h2_block,
    h2_kinds,
    images_endpoint,
    node_pubkey_hex,
    pin_offer,
    pricing_fields,
    release,
    reserve,
    reserve_deposit,
    reserve_image,
    settle,
    settle_flat,
    sidecar_framings,
    sidecar_headers,
    sign_payload,
    to_ws_url,
    unpin_offer,
)
from .core import get_logger
from .core.lifecycle import RequestLifetime, request_lifetime
from .core.settings import settings

logger = get_logger(__name__)

confidential_ws_router = APIRouter()

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# The zk route is keyed by the protocol sid (a 32-byte hex digest).
_SID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
# Protocol v2 only (v1's pi_c1/pi_n/pi_c2/pi_c3 are retired).
# ``pi_win`` proves the client's records of an ``http: 2`` session.
_PROOF_RE = re.compile(r"^(twopc|zkchan|pi_nk|pi_c3s|pi_win)$")
# The proof/2PC channel carries mux frames of at most 1 MiB; the session
# channel carries TLS records (≤ 16.6 KiB each), which a client may batch, at
# most a whole declared body (8 MiB).
_WS_MAX_SIZE = 64 << 20
_SESSION_WS_MAX_SIZE = 12 << 20

# Limits: concurrent sessions per process (counted once the setup's
# reservation is held), time to the setup frame (clients send it as soon as
# the socket opens), a session's lifetime until the final body record is
# released (counted from the WebSocket accept), the window after that release
# for the response and the usage disclosure, and the largest request body a
# setup may declare.
MAX_SESSIONS = 32
SETUP_TIMEOUT_S = 10.0
SESSION_TIMEOUT_S = 600.0
POST_RELEASE_TIMEOUT_S = 600.0
MAX_BODY_LEN = 8 << 20
# The setup frame is parsed before any credential is checked: anything larger
# than a setup with a large Cashu token is refused unparsed. (uvicorn's
# ``--ws-max-size`` bounds what reaches the node at all.)
MAX_SETUP_TEXT = 256 << 10
# How long a session cut off at its time limit waits, after closing the
# sidecar connection, for the sidecar's last frames (a ``body_released`` or
# a usage already on its way), so the final charge sees its real state.
SIDECAR_DRAIN_S = 5.0
# Time after the session limit for the final settlement or abort to finish.
# The reservation's lease covers both phases plus this, whatever
# MAX_REQUEST_LIFETIME_SECONDS is: a lease that lapsed mid-session would let
# the stale-reservation sweeper release it, and the final charge would fail.
SETTLE_GRACE_S = 60.0
# Connections whose setup is not reserved yet (waiting for the frame, or the
# reservation in progress) are pending: they never take a session slot. The
# pool is large, each address (IPv6: its /64) holds at most a few, and a full
# pool or address share evicts the oldest connection still waiting for its
# setup, so idle sockets cannot keep a credentialed setup out.
MAX_PENDING = 1024
MAX_PENDING_PER_ADDR = 4
# Concurrent /zk proxies per session (the sidecar's own limit) and per
# process. The per-session limit is enforced here too, so one session cannot
# hold the process-wide slots other sessions' proof channels need.
MAX_ZK_CONNS_PER_SESSION = 6
MAX_ZK_CONNS = MAX_SESSIONS * MAX_ZK_CONNS_PER_SESSION
# Longest a /zk proxy may stay open; it normally closes with its session.
_ZK_PROXY_CAP_S = SESSION_TIMEOUT_S + POST_RELEASE_TIMEOUT_S + SETTLE_GRACE_S
# The setup fields the sidecar is sent (``escrow`` is set by the node; for an
# image session also ``kind``, ``count`` and the node-rendered ``suffix``).
_SETUP_KINDS = ("chat", "images")
# ``http: 2`` sessions (protocol v3, option A). What a setup must not name
# (it belongs to the requests), what of a ``request`` the sidecar is sent,
# how long past the sidecar's own idle limit the node waits before it ends an
# idle connection itself, and the sidecar's limit on a connection's age (a
# backstop for the connection's proof channels here).
_H2_SETUP_FORBIDDEN = (
    "kind",
    "model",
    "max_tokens",
    "len",
    "body_len",
    "suffix",
    "count",
    "variant",
    "session_fee_msat",
    "abandon_fee_msat",
)
_H2_REQUEST_FIELDS = ("type", "n", "kind", "model", "max_tokens", "body_len", "suffix")
# An image request: the sidecar gets the count and the suffix the node
# rendered from its own signed offer (never one the client sent).
_H2_IMAGE_REQUEST_FIELDS = ("type", "n", "model", "body_len")
H2_CONN_MAX_S = 3600.0
# Why a connection's deposit was released although no request of it reached
# the provider: the failure was the node's, the sidecar's or the provider's.
# The sidecar names its own in an ``error`` frame's ``fault``.
_H2_SIDECAR_FAULTS = {"upstream": "provider_unreachable", "node": "sidecar_failure"}

_active_sessions = 0
_active_zk = 0
# Pending connections, oldest first (pool-wide and per address bucket).
_pending: dict["SessionState", str] = {}
_pending_by_addr: dict[str, dict["SessionState", None]] = {}
# Protocol sids of this process's live sessions (from the sidecar's ``ready``
# until the session ends): only these may open a /zk proxy.
_live_sids: dict[str, "SessionState"] = {}
# Unused sessions per client address bucket: counted from the reservation
# until the sidecar reports ``head_written`` or the session ends.
_unused_by_ip: dict[str, int] = {}


# The open lock file that marks this process as the node's only worker.
_worker_lock_fd: int | None = None


def _configured_workers(
    argv: list[str], env: dict[str, str], gunicorn: bool = False
) -> int:
    """The worker count named by the launcher's arguments or environment
    (uvicorn ``--workers``/``WEB_CONCURRENCY``, gunicorn ``-w``/``--workers``,
    also as ``-w4``/``-w=4`` and in ``GUNICORN_CMD_ARGS``)."""
    found = [env.get("WEB_CONCURRENCY", "")]
    gunicorn = (
        gunicorn
        or "gunicorn" in (argv[0] if argv else "")
        or "gunicorn" in env.get("SERVER_SOFTWARE", "")
    )
    args = argv[1:] + env.get("GUNICORN_CMD_ARGS", "").split()
    for i, arg in enumerate(args):
        if arg.startswith("--workers="):
            found.append(arg.split("=", 1)[1])
        elif arg == "--workers" or (arg == "-w" and gunicorn):
            found.append(args[i + 1] if i + 1 < len(args) else "")
        elif gunicorn and arg.startswith("-w"):
            found.append(arg[2:].removeprefix("="))
    counts = [int(v) for v in found if v.strip().isdigit()]
    return max(counts, default=1)


def _private_lock_dir() -> str:
    """A directory only this user can write, for the worker lock.

    A lock file in a shared temporary directory could be created first (or
    swapped for a symlink) by another local account: it could then keep the
    node from starting, or let a second process pass the guard."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    path = os.path.join(base, f"routstr-confidential-{os.getuid()}")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError(
            f"{path} is not a private directory of this user (a directory, "
            "owned by it, mode 0700); remove it or point TMPDIR elsewhere"
        )
    return path


def claim_single_worker(database_url: str) -> None:
    """Fail closed unless this is the node's only worker process.

    Signed offers, live sessions and the /zk admission are per process, so a
    second worker would refuse other workers' offers and proof channels and
    double every limit. A launcher that names more than one worker is refused,
    and an exclusive lock (per sidecar and database) catches any other
    process of the same node on this host. Raises ``RuntimeError``.
    """
    global _worker_lock_fd
    workers = _configured_workers(
        sys.argv, dict(os.environ), gunicorn="gunicorn" in sys.modules
    )
    if workers > 1:
        raise RuntimeError(
            f"confidential mode needs a single worker process, but {workers} "
            "are configured (--workers / WEB_CONCURRENCY); run one worker or "
            "unset CONFIDENTIAL_SIDECAR_URL"
        )
    if _worker_lock_fd is not None:
        return
    try:
        import fcntl
    except ImportError:  # not POSIX: only the launcher check applies
        return
    key = f"{settings.confidential_sidecar_url}\n{database_url}".encode()
    path = os.path.join(
        _private_lock_dir(), f"{hashlib.sha256(key).hexdigest()[:16]}.lock"
    )
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise RuntimeError(f"cannot open the worker lock {path}: {e}") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(
            "confidential mode needs a single worker process, but another "
            f"process of this node holds {path}; run one worker"
        ) from None
    _worker_lock_fd = fd


def _sidecar_ws_url(path: str, query: str) -> str:
    base = to_ws_url(settings.confidential_sidecar_url.rstrip("/"))
    return f"{base}{path}?{query}"


def _short(sid: str | None) -> str:
    return (sid or "")[:12]


class SessionState:
    def __init__(self) -> None:
        self.billing: Billing | None = None
        # The client's address bucket (``_address_bucket``).
        self.addr_key = "unknown"
        # Client address bucket counted in ``_unused_by_ip`` until the session
        # is used.
        self.unused_ip: str | None = None
        # Pending (in ``_pending``) until the setup is reserved or fails; only
        # a connection still waiting for its setup frame may be evicted.
        self.awaiting_setup = True
        self.evicted = asyncio.Event()
        # Holds one of the MAX_SESSIONS slots.
        self.counted = False
        self.sid: str | None = None
        self.body_released = False
        # Loop time of ``body_released``: starts the post-release window.
        self.released_at: float | None = None
        # The sidecar accepted the setup (``ready``): from here on the node is
        # doing the session's 2PC work and an abandoned session keeps its fee.
        self.engaged = False
        # The sidecar reported that the provider closed the connection after
        # answering, before the final body record was released (a refusal
        # from the head alone): the session fee is charged, not the abandon fee.
        self.upstream_refused = False
        # Set once the reservation is charged or released; ``lock`` makes
        # settlement and abort handling finalize it exactly once.
        self.finalized = False
        self.lock = asyncio.Lock()
        self.attestation_hash = ""
        # Set when the session ends: its /zk proxies close with it.
        self.ended = asyncio.Event()
        # Admits this session's /zk channels, together with the sid. Only the
        # client gets it (in ``ready``); unlike the sid it is in no receipt or
        # attestation, so a shared receipt or log line cannot open a channel.
        self.zk_token = secrets.token_hex(32)
        # Open /zk proxies of this session (``MAX_ZK_CONNS_PER_SESSION``).
        self.zk_open = 0
        # An ``http: 2`` session: one connection, sequential requests. The
        # fields above that describe a reservation (``billing``,
        # ``body_released``, ``released_at``, ``engaged``, ``upstream_refused``,
        # ``finalized``, ``attestation_hash``) then describe the request in
        # flight, and ``billing`` is ``None`` between requests.
        # The framing of the inner connection (``setup.http``): "2" or "1.1".
        self.framing = "2"
        # The signed offer the connection was opened on (kept remembered
        # while the connection lives), and its terms.
        self.offer_sig: str | None = None
        self.offer_terms: Any = None
        # Number of the last request forwarded to the sidecar, and how many
        # were settled with a receipt.
        self.h2_n = 0
        self.h2_settled = 0
        # ``request`` frames from the client pump, each with the future that
        # receives the frame to forward (``None``: refused).
        self.h2_requests: asyncio.Queue[tuple[Any, asyncio.Future[str | None]]] = (
            asyncio.Queue()
        )
        # Loop times: the request in flight was reserved; the connection
        # became idle (``head_written`` or the last settlement).
        self.h2_request_at = 0.0
        self.h2_idle_since: float | None = None
        # The sidecar's idle limit for the connection (the offer's ``h2`` block).
        self.h2_idle_secs = 300.0
        # The lifetime the request in flight's reservation lease was taken on.
        self.h2_lifetime: RequestLifetime | None = None
        # Set by the sidecar pump when the session's deadline moved
        # (``head_written``, ``body_released``, a settlement).
        self.h2_moved = asyncio.Event()
        # Seconds without a request in flight after which the node closes the
        # connection (the signed offer's ``h2.idle_timeout_s``).
        self.h2_idle_timeout = 120.0
        # The connection deposit: reserved at the setup, released when the
        # first request reaches settlement, charged if the connection ends
        # without one (``_h2_deposit_verdict``). ``None`` once finalized, or
        # when the offer asks for none.
        self.h2_deposit: Billing | None = None
        self.h2_deposit_msat = 0
        # The sidecar accepted the setup (``ready``): the node garbles from here.
        self.h2_ready = False
        # The sidecar's last frame was ``error`` or ``closed`` (an end it
        # explained), and the reason of a ``closed``.
        self.h2_terminal = False
        self.h2_closed_reason: str | None = None
        self.h2_last_frame: dict[str, Any] | None = None
        # A failure of the node, the sidecar or the provider that ended the
        # connection before the client could get a request through.
        self.h2_fault: str | None = None
        # The outcome of a deposit released because the setup failed after
        # it was reserved (no slot, the sidecar unreachable).
        self.h2_setup_deposit: dict[str, Any] | None = None
        # The sidecar's socket ended (as opposed to the client's).
        self.h2_sidecar_gone = False
        # The node's own last frame after a failure of its own (a settlement
        # that could not be written): the session task sends it, with the
        # deposit's outcome, and ends the connection.
        self.h2_node_error: dict[str, Any] | None = None
        # The longest a framing-"2" connection lives (the offer's
        # ``h2.max_lifetime_s``): the backstop of its proof channels.
        self.h2_max_lifetime = H2_CONN_MAX_S


class SetupError(Exception):
    # A refused ``request`` leaves the connection usable (``request_refused``)
    # unless it is a protocol violation (``fatal``).
    fatal = False

    def __init__(self, status: int, reason: str, detail: Any = None) -> None:
        super().__init__(reason)
        self.status = status
        self.detail = detail if detail is not None else reason


class ProtocolError(SetupError):
    """A ``request`` that breaks the protocol (a wrong ``n``, a second request
    in flight): it ends the connection."""

    fatal = True


def _error_frame(status: int, reason: str, detail: Any = None) -> dict[str, Any]:
    return {
        "type": "error",
        "status": status,
        "reason": reason,
        "detail": detail if detail is not None else reason,
    }


async def _send_error(
    websocket: WebSocket, status: int, reason: str, detail: Any = None
) -> None:
    try:
        await websocket.send_text(json.dumps(_error_frame(status, reason, detail)))
    except Exception:
        pass


def _node_error(
    state: "SessionState", status: int, reason: str, detail: Any = None
) -> None:
    """The node itself failed: its ``error`` frame becomes the connection's
    last frame (sent by the session task, with the deposit's outcome)."""
    frame = _error_frame(status, reason, detail)
    frame["fault"] = "node"
    state.h2_node_error = state.h2_node_error or frame
    state.h2_moved.set()


async def _upgrade_required(websocket: WebSocket) -> None:
    try:
        await websocket.send_text(json.dumps(UPGRADE_REQUIRED))
    except Exception:
        pass
    await _close(websocket, 1008, "upgrade_required")


async def _close(ws: Any, code: int = 1008, reason: str = "") -> None:
    try:
        await ws.close(code=code, reason=reason[:120])
    except Exception:
        pass


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_billing_fields(msg: dict[str, Any], what: str) -> None:
    """``offer_sig``, ``auth`` and the accepted fees of a setup or request."""
    offer_sig = msg.get("offer_sig")
    if not isinstance(offer_sig, str) or not _HEX_RE.fullmatch(offer_sig):
        raise SetupError(
            400, f"{what}.offer_sig must be the hex signature of the verified offer"
        )
    if not isinstance(msg.get("auth", ""), str):
        raise SetupError(400, f"{what}.auth must be a string")
    for name in ("session_fee_msat", "abandon_fee_msat"):
        fee = msg.get(name)
        if fee is not None and (not _is_int(fee) or fee < 0):
            raise SetupError(400, f"{what}.{name} must be a non-negative integer")


def validate_h2_setup(msg: Any) -> None:
    """Shape checks on a setup: the connection only (``v``, the framing), no
    request terms and no fees (both belong to its requests)."""
    if not isinstance(msg, dict) or msg.get("type") != "setup":
        raise SetupError(400, "the first frame must be setup")
    http = msg.get("http")
    if not isinstance(http, str) or http not in FRAMINGS:
        raise SetupError(400, 'setup.http must be "2" or "1.1"')
    named = [k for k in _H2_SETUP_FORBIDDEN if k in msg]
    if named:
        raise SetupError(
            400,
            f"a setup names no {', '.join(named)}: "
            "request terms belong to its requests",
        )
    nonce_c = msg.get("nonce_c")
    if not isinstance(nonce_c, str) or not _SID_RE.fullmatch(nonce_c):
        raise SetupError(400, "setup.nonce_c must be 64 hex characters")
    _validate_billing_fields(msg, "setup")
    if not isinstance(msg.get("armed", False), bool):
        raise SetupError(400, "setup.armed must be a boolean")


def validate_h2_request(msg: Any, max_tokens_cap: int, expected_n: int) -> None:
    """Shape and range checks on a ``request`` of an ``http: 2`` session,
    before anything is reserved."""
    if not isinstance(msg, dict) or msg.get("type") != "request":
        raise SetupError(400, "not a request")
    n = msg.get("n")
    if not _is_int(n) or n != expected_n:
        raise ProtocolError(400, f"request.n must be {expected_n}")
    kind = msg.get("kind", "chat")
    if kind not in _SETUP_KINDS:
        raise SetupError(400, "request.kind must be chat or images")
    model = msg.get("model")
    if not isinstance(model, str) or not model:
        raise SetupError(400, "request.model must be a non-empty string")
    if kind == "images":
        variant = msg.get("variant")
        if not isinstance(variant, str) or not variant:
            raise SetupError(400, "request.variant must be a non-empty string")
        count = msg.get("count")
        if not _is_int(count) or not 1 <= count <= IMAGE_MAX_COUNT:
            raise SetupError(
                400, f"request.count must be an integer in [1, {IMAGE_MAX_COUNT}]"
            )
        if "max_tokens" in msg:
            raise SetupError(400, "an image request takes no max_tokens")
    else:
        max_tokens = msg.get("max_tokens")
        if not _is_int(max_tokens) or not 1 <= max_tokens <= max_tokens_cap:
            raise SetupError(
                400,
                f"request.max_tokens must be an integer in [1, {max_tokens_cap}]",
            )
        if "variant" in msg or "count" in msg:
            raise SetupError(400, "a chat request takes no variant or count")
    length = msg.get("body_len")
    if not _is_int(length) or not 1 <= length <= MAX_BODY_LEN:
        raise SetupError(
            400, f"request.body_len must be an integer in [1, {MAX_BODY_LEN}]"
        )
    if not isinstance(msg.get("suffix", ""), str):
        raise SetupError(400, "request.suffix must be a string")
    _validate_billing_fields(msg, "request")


def _session_lifetime() -> RequestLifetime:
    """The lifetime the session's reservation lease is derived from: both
    session phases plus the settlement grace, never shorter than the HTTP
    request lifetime."""
    seconds = max(
        float(settings.max_request_lifetime_seconds),
        SESSION_TIMEOUT_S + POST_RELEASE_TIMEOUT_S + SETTLE_GRACE_S,
    )
    return RequestLifetime(deadline=asyncio.get_running_loop().time() + seconds)


@confidential_ws_router.websocket("/v1/confidential/ws")
async def confidential_session_proxy(
    websocket: WebSocket, session_id: str = "", v: str = ""
) -> None:
    global _active_sessions
    await websocket.accept()
    if v != "3":
        # A v2 (or unversioned) client: told to upgrade, in a frame it can
        # show, then closed (``PROTOCOL-V3.md`` §11).
        await _upgrade_required(websocket)
        return
    if not _UUID_RE.fullmatch(session_id or ""):
        await websocket.close(code=1008, reason="invalid session_id")
        return
    if not node_pubkey_hex():
        await websocket.close(code=1011, reason="node has no signing identity")
        return
    # The session slots and the unused limit are taken only once the setup is
    # reserved (``_claim_slot``); this early check just spares a reservation
    # that would be refused anyway.
    key = _address_bucket(client_address(websocket))
    limit = settings.confidential_max_unused_per_ip
    if limit > 0 and _unused_by_ip.get(key, 0) >= limit:
        await _send_error(
            websocket, 429, f"too many unused confidential sessions (limit {limit})"
        )
        await _close(websocket, 1013, "too many unused confidential sessions")
        return
    state = SessionState()
    refusal = _admit_pending(state, key)
    if refusal is not None:
        await _send_error(websocket, refusal[0], refusal[1])
        await _close(websocket, 1013, refusal[1])
        return
    lifetime = request_lifetime.set(_session_lifetime())
    try:
        await _run_session(websocket, session_id, state)
    finally:
        request_lifetime.reset(lifetime)
        _release_pending(state)
        if state.counted:
            state.counted = False
            _active_sessions -= 1
        _mark_used(state)
        _forget_sid(state)


def _address_bucket(addr: str) -> str:
    """The key of the per-address limits: an IPv4 address as is, an IPv6
    address by its /64 (one host or subscriber usually holds a whole /64)."""
    try:
        ip = ipaddress.ip_address(addr)
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None:
                return str(ip.ipv4_mapped)
            return str(ipaddress.IPv6Network((ip.packed, 64), strict=False))
        return str(ip)
    except ValueError:
        return addr


def _oldest_waiting(pool: dict["SessionState", Any]) -> "SessionState | None":
    return next((s for s in pool if s.awaiting_setup), None)


def _admit_pending(state: "SessionState", key: str) -> tuple[int, str] | None:
    """Add a new connection to the pending pool, evicting the oldest one
    still waiting for its setup when the address's share or the pool is full.
    Returns ``(status, reason)`` when nothing can be evicted (every pending
    connection's setup is being reserved)."""
    bucket = _pending_by_addr.get(key, {})
    if len(bucket) >= MAX_PENDING_PER_ADDR:
        victim = _oldest_waiting(bucket)
        if victim is None:
            return 429, "too many pending confidential sessions from this address"
        _evict(victim)
    if len(_pending) >= MAX_PENDING:
        victim = _oldest_waiting(_pending)
        if victim is None:
            return 503, "too many pending confidential sessions"
        _evict(victim)
    state.addr_key = key
    _pending[state] = key
    _pending_by_addr.setdefault(key, {})[state] = None
    return None


def _evict(state: "SessionState") -> None:
    _release_pending(state)
    state.evicted.set()


def _release_pending(state: "SessionState") -> None:
    """The connection no longer counts as pending (idempotent)."""
    key = _pending.pop(state, None)
    if key is None:
        return
    bucket = _pending_by_addr.get(key)
    if bucket is not None:
        bucket.pop(state, None)
        if not bucket:
            del _pending_by_addr[key]


def _claim_slot(state: "SessionState") -> None:
    """Count a reserved session against MAX_SESSIONS and the unused limit.
    Raises ``SetupError``; the caller then releases the reservation (in full:
    the sidecar has not been dialed)."""
    global _active_sessions
    _release_pending(state)
    if _active_sessions >= MAX_SESSIONS:
        raise SetupError(503, "too many confidential sessions")
    key = state.addr_key
    limit = settings.confidential_max_unused_per_ip
    if limit > 0 and _unused_by_ip.get(key, 0) >= limit:
        raise SetupError(429, f"too many unused confidential sessions (limit {limit})")
    _active_sessions += 1
    state.counted = True
    state.unused_ip = key
    _unused_by_ip[key] = _unused_by_ip.get(key, 0) + 1


def _forget_sid(state: "SessionState") -> None:
    state.ended.set()
    if state.sid is not None and _live_sids.get(state.sid) is state:
        del _live_sids[state.sid]


def _mark_used(state: "SessionState") -> None:
    """The session no longer counts against its address's unused limit."""
    ip, state.unused_ip = state.unused_ip, None
    if ip is None:
        return
    left = _unused_by_ip.get(ip, 1) - 1
    if left > 0:
        _unused_by_ip[ip] = left
    else:
        _unused_by_ip.pop(ip, None)


def _own_addresses() -> set[str]:
    addrs = {"127.0.0.1", "::1"}
    try:
        host = socket.gethostname()
        addrs.update(socket.gethostbyname_ex(host)[2])
        for info in socket.getaddrinfo(host, None):
            addrs.add(str(info[4][0]))
    except OSError:
        pass
    return addrs


def client_address(websocket: WebSocket) -> str:
    """The client's address for the per-address limit.

    The peer itself, unless the peer is a trusted proxy
    (``confidential_trusted_proxies``); then the proxy's header
    (``confidential_client_ip_header``; for X-Forwarded-For its rightmost entry,
    which the proxy appended; entries to its left come from the client).
    """
    peer = websocket.client.host if websocket.client else ""
    trusted = set(settings.confidential_trusted_proxies)
    if "self" in trusted:
        trusted |= _own_addresses()
    if peer in trusted:
        name = settings.confidential_client_ip_header.strip().lower()
        lines = websocket.headers.getlist(name)
        if name == "x-forwarded-for":
            # Repeated lines form one list, in order (a proxy may add its own
            # line rather than append to the client's): the proxy's entry is
            # the last of all of them.
            value = ",".join(lines).split(",")[-1]
        else:
            # A single-value header: the line the proxy added is the last.
            value = lines[-1] if lines else ""
        value = value.strip()
        if value:
            return value
    return peer or "unknown"


async def _run_session(
    websocket: WebSocket, session_id: str, state: SessionState | None = None
) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    state = state or SessionState()
    try:
        upstream = await _open_session(websocket, session_id, state)
        if upstream is None:
            return
        await _run_h2_session(websocket, upstream, state, started)
    finally:
        # The offer the connection was opened on is remembered until here.
        if state.offer_sig is not None:
            unpin_offer(state.offer_sig)
            state.offer_sig = None


async def _drain_sidecar(upstream: Any, sidecar_pump: Any, timeout: float) -> None:
    """Close the sidecar connection and let ``sidecar_pump`` forward the frames
    the sidecar sent before its close, for at most ``timeout`` seconds."""
    closing = asyncio.ensure_future(_close(upstream, 1000))
    await asyncio.wait({sidecar_pump}, timeout=timeout)
    if not closing.done():
        closing.cancel()


async def _open_session(
    websocket: WebSocket, session_id: str, state: SessionState
) -> Any:
    """Wait for ``setup``, validate it, reserve, then dial the sidecar.

    Returns the sidecar connection, or ``None`` after reporting an error.
    """
    try:
        receive = asyncio.ensure_future(websocket.receive())
        evicted = asyncio.ensure_future(state.evicted.wait())
        try:
            done, _ = await asyncio.wait(
                {receive, evicted},
                timeout=SETUP_TIMEOUT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (receive, evicted):
                if not task.done():
                    task.cancel()
        if receive not in done:
            if evicted in done:
                # A newer connection needed the pending slot.
                raise SetupError(503, "too many pending confidential sessions")
            raise SetupError(408, "no setup frame received in time")
        message = receive.result()
        # From here on the connection's setup is being reserved: it stays
        # pending (bounding concurrent reservations) but is never evicted.
        state.awaiting_setup = False
        if message["type"] == "websocket.disconnect":
            return None
        text = message.get("text") or ""
        if len(text) > MAX_SETUP_TEXT or len(text.encode("utf-8")) > MAX_SETUP_TEXT:
            raise SetupError(413, f"setup frame larger than {MAX_SETUP_TEXT} bytes")
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            msg = None
        if isinstance(msg, dict) and msg.get("type") == "setup":
            v = msg.get("v")
            if isinstance(v, bool) or v != 3:
                # A v2-shaped setup on a socket that claimed v3.
                await _upgrade_required(websocket)
                return None
        try:
            side = await cached_sidecar_offer()
        except Exception as exc:
            logger.warning("confidential ws: sidecar offer unavailable: %s", exc)
            raise SetupError(503, "confidential sidecar unavailable") from exc
        return await _open_h2_session(session_id, state, msg, side)
    except (WebSocketDisconnect, RuntimeError):
        await _finalize(state)
        return None
    except SetupError as exc:
        logger.warning("confidential ws: rejecting session: %s", exc)
        await _finalize(state)
        # A deposit that was reserved and released again (the sidecar could
        # not be dialed): the refusal says so, signed.
        frame = _error_frame(exc.status, str(exc), exc.detail)
        try:
            await websocket.send_text(_with_deposit(frame, state.h2_setup_deposit))
        except Exception:
            pass
        await _close(websocket, 1008, str(exc))
        return None


async def _dial_sidecar(session_id: str, setup: dict[str, Any]) -> Any:
    """Open the sidecar's session socket and send it ``setup``."""
    try:
        upstream = await websockets.connect(
            _sidecar_ws_url("/session", f"session_id={session_id}"),
            extra_headers=sidecar_headers(),
            max_size=_SESSION_WS_MAX_SIZE,
            open_timeout=10,
        )
    except Exception as exc:
        raise SetupError(502, "confidential sidecar unreachable") from exc
    try:
        await upstream.send(json.dumps(setup))
    except Exception as exc:
        await _close(upstream, 1011)
        raise SetupError(502, "confidential sidecar unreachable") from exc
    return upstream


async def _sidecar_to_client(
    websocket: WebSocket, upstream: Any, state: SessionState, close_client: bool = True
) -> None:
    """Relay the sidecar's frames to the client. ``close_client`` False leaves
    the client's socket open when the sidecar's ends (an ``http: 2`` session
    still has its last frames to send)."""
    try:
        async for frame in upstream:
            if isinstance(frame, bytes):
                await websocket.send_bytes(frame)
                continue
            outgoing = await _on_sidecar_text(frame, websocket, state)
            if outgoing is not None:
                await websocket.send_text(outgoing)
        # The sidecar's socket ended (not the client's).
        state.h2_sidecar_gone = True
    except websockets.ConnectionClosed:
        # Only the sidecar's socket raises this: it ended without a close.
        state.h2_sidecar_gone = True
    except (WebSocketDisconnect, RuntimeError):
        # The client's socket ended: nothing more can be sent to it.
        pass
    finally:
        if close_client:
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
        sid = msg.get("sid")
        state.sid = sid if isinstance(sid, str) else None
        state.engaged = True
        if state.sid is not None and _SID_RE.fullmatch(state.sid):
            # Registered before the client sees ``ready``, so its /zk
            # connections (opened with this sid) are admitted.
            _live_sids.setdefault(state.sid, state)
        msg.pop("offer", None)
        state.h2_ready = True
        # What the setup reserved (the signed offer's deposit).
        msg["deposit_msat"] = state.h2_deposit_msat
        msg["zk_token"] = state.zk_token
        msg["pubkey"] = node_pubkey_hex()
        msg["sig_scheme"] = "schnorr-secp256k1"
        return json.dumps(msg)
    if kind == "head_written":
        # The connection is open (framing "2": idle; "1.1": request 1 goes
        # on). It stays "unused" until a request reaches the provider.
        state.h2_idle_since = asyncio.get_running_loop().time()
        state.h2_moved.set()
        return frame
    if kind in ("error", "closed"):
        state.h2_terminal = True
        if kind == "closed":
            reason = msg.get("reason")
            state.h2_closed_reason = reason if isinstance(reason, str) else "closed"
            if reason == "upstream_closed" and state.billing is None:
                # The provider closed an idle connection: not the client's doing.
                state.h2_fault = state.h2_fault or "provider_closed"
        else:
            fault = _H2_SIDECAR_FAULTS.get(str(msg.get("fault")))
            if fault is not None:
                state.h2_fault = state.h2_fault or fault
        # Held back: the session task sends it as the connection's last
        # frame, with the deposit's outcome.
        state.h2_last_frame = msg
        state.h2_moved.set()
        return None
    if kind == "request_refused":
        # The sidecar does not serve the request's terms: the reservation is
        # released in full, the client is told, the connection stays.
        if state.billing is None or msg.get("n") != state.h2_n:
            return None
        await asyncio.shield(_h2_request_refused(state))
        return frame
    if kind in ("request_ok", "body_released", "attestation", "usage"):
        n = msg["a"].get("n") if isinstance(msg.get("a"), dict) else msg.get("n")
        if state.billing is None or n != state.h2_n:
            # Not about the request in flight: nothing to bill or sign.
            logger.warning(
                "confidential ws: dropping a frame for another request",
                extra={"sid": _short(state.sid), "frame": kind},
            )
            return None
        if kind == "request_ok":
            # The sidecar accepted the request: from here an abandoned
            # request keeps its fee, as a v2 session does after ``ready``.
            state.engaged = True
            return frame
    if kind == "upstream_closed_early":
        # The provider closed before the final body record was released. With
        # response bytes it answered first (a refusal from the head alone,
        # which the client cannot disclose); with none it never answered.
        n = msg.get("response_bytes")
        if _is_int(n) and n > 0 and not state.body_released:
            state.upstream_refused = True
        logger.info(
            "confidential ws: provider closed before the request was complete",
            extra={
                "sid": _short(state.sid),
                "response_bytes": n if _is_int(n) else None,
            },
        )
        return frame
    if kind == "body_released":
        state.body_released = True
        if state.released_at is None:
            state.released_at = asyncio.get_running_loop().time()
        # The connection carried a complete request: it is in use.
        _mark_used(state)
        state.h2_moved.set()
        return frame
    if kind == "attestation" and msg.get("a") is not None:
        a = msg["a"]
        if state.sid is None or not isinstance(a, dict) or a.get("sid") != state.sid:
            # The node signs only an attestation of this session.
            logger.warning(
                "confidential ws: dropping an attestation for another sid",
                extra={"sid": _short(state.sid)},
            )
            return None
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
        if state.billing is not None and state.h2_node_error is None:
            # No receipt was signed and nothing was said: the connection
            # ends, and ``_finalize`` sees what is left.
            _node_error(state, 500, "settlement failed")
        return None
    return frame


def _fee(billing: Billing, name: str) -> int:
    """``session_fee_msat`` or ``abandon_fee_msat`` of the frozen offer, ≤ reserved."""
    fee = getattr(billing, name, 0) or 0
    if name == "abandon_fee_msat":
        fee = max(fee, getattr(billing, "session_fee_msat", 0) or 0)
    return max(0, min(fee, billing.reserved_msats))


async def _release_keeping_fee(billing: Billing, name: str) -> tuple[int, int | None]:
    """Release the reservation but keep one fee: (cost, balance)."""
    fee = _fee(billing, name)
    if fee == 0:
        return 0, await release(billing)
    return fee, await charge_fee(billing, fee)


async def _apply_settlement(
    billing: Billing,
    usage: dict[str, Any],
    model: Any = None,
    reported_model: Any = None,
) -> tuple[int, int | None]:
    """Finalize the reservation for a disclosed usage event: (cost, balance).

    ``model`` is the usage frame's top-level model (a flat image settlement
    carries it there rather than in ``usage``); ``reported_model`` is the
    model the provider's usage event named when it differs from the pinned
    one (sent by newer sidecars; older ones put the provider's spelling in
    ``model``)."""
    images = getattr(billing, "kind", "chat") == "images"
    if usage.get("flat") is True:
        reported = usage.get("model", model)
        if not images or (reported is not None and reported != billing.model_id):
            # A flat settlement on a chat session (or for another model) is a
            # protocol breach: the request reached the provider unpriced.
            return billing.reserved_msats, await charge_reservation(billing)
        settled = await settle_flat(billing)
        return int(settled["cost_msats"]), settled["balance_msats"]
    error_status = usage.get("error_status")
    if _is_int(error_status):
        if error_status in UNBILLED_ERROR_STATUSES:
            # The provider answered before doing inference. Its own state
            # (rate limit, capacity) costs the client the session fee only;
            # a request it rejected costs the abandon fee, since the node
            # did the session's 2PC for a request that could not run.
            if error_status in PROVIDER_REFUSAL_STATUSES:
                return await _release_keeping_fee(billing, "session_fee_msat")
            return await _release_keeping_fee(billing, "abandon_fee_msat")
        # 500/502/504 …: work may have been billed upstream.
        return billing.reserved_msats, await charge_reservation(billing)
    if images:
        # Image sessions settle flat or on an error head, never on token
        # usage: anything else is a protocol breach.
        return billing.reserved_msats, await charge_reservation(billing)
    # π_WIN proved the request names the pinned model, so a usage event that
    # spells it differently (an alias, a canonical id, a case change) is
    # still that request: it is settled at the pinned model's frozen rates.
    reported = reported_model if reported_model is not None else usage.get("model")
    if reported != billing.model_id:
        logger.warning(
            "confidential ws: provider reported another model than the pinned "
            "one; settling at the pinned model's rates",
            extra={
                "model": billing.model_id,
                "reported_model": str(reported)[:200],
            },
        )
    settled = await settle(billing, usage)
    return int(settled["cost_msats"]), settled["balance_msats"]


# Pauses before retrying a settlement whose database write failed.
SETTLE_RETRY_DELAYS = (0.2, 1.0, 3.0)


async def _apply_settlement_retrying(
    billing: Billing,
    usage: dict[str, Any],
    model: Any = None,
    reported_model: Any = None,
) -> tuple[int, int | None]:
    """``_apply_settlement``, retried on a failure of the node's own (a busy or
    dropped database). The usage is already verified, so a retry repeats only
    the billing write; the reservation's claim is atomic with the charge, so a
    write that did commit is never charged twice (the retry then fails, and
    the caller's fallback finds nothing left to finalize)."""
    for delay in SETTLE_RETRY_DELAYS:
        try:
            return await _apply_settlement(billing, usage, model, reported_model)
        except Exception:
            logger.warning(
                "confidential ws: settlement failed, retrying",
                exc_info=True,
                extra={"model": billing.model_id, "retry_in_s": delay},
            )
            await asyncio.sleep(delay)
    return await _apply_settlement(billing, usage, model, reported_model)


async def _settle(
    msg: dict[str, Any], websocket: WebSocket, state: SessionState
) -> None:
    billing = state.billing
    if billing is None:
        return
    raw = msg.get("usage")
    usage: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if not state.attestation_hash:
        # The sidecar attests before it accepts a disclosure, so this is a
        # fault on the node's side, and no receipt could be bound to the
        # session. Fail closed: charge nothing, sign nothing.
        logger.error(
            "confidential ws: usage without an attestation",
            extra={"sid": _short(state.sid)},
        )
        await _refuse_unattested(websocket, state)
        return
    async with state.lock:
        if state.finalized:
            return
        try:
            cost, balance = await _apply_settlement_retrying(
                billing, usage, msg.get("model"), msg.get("reported_model")
            )
        except Exception:
            logger.exception(
                "confidential ws: settlement failed",
                extra={"sid": _short(state.sid), "model": billing.model_id},
            )
            outcome = await _finalize_unsettled(state)
            state.h2_fault = state.h2_fault or "node_failure"
            _node_error(
                state,
                500,
                "settlement failed",
                {"error": {"type": "settlement_failed", "outcome": outcome}},
            )
            return
        state.finalized = True
    receipt: dict[str, Any] = {
        "v": 1,
        "sid": state.sid,
        "model": billing.model_id,
        "usage": usage,
        "cost_msats": cost,
        "reserved_msats": billing.reserved_msats,
        "balance_msats": balance,
        **pricing_fields(
            getattr(billing, "session_fee_msat", 0),
            getattr(billing, "abandon_fee_msat", 0),
            getattr(billing, "rate_surcharge_pct", 0.0),
        ),
        "notary_pubkey": node_pubkey_hex(),
        "attestation_hash": state.attestation_hash,
        "time": int(time.time()),
    }
    if getattr(billing, "kind", "chat") == "images":
        receipt.update(
            kind="images",
            variant=billing.variant,
            count=billing.count,
            msat_per_image=billing.msat_per_image,
        )
    else:
        receipt["rates"] = billing.rates
    receipt["http"] = state.framing
    receipt["n"] = state.h2_n
    log_extra: dict[str, Any] = {"n": state.h2_n, "http": state.framing}
    if state.h2_deposit is not None:
        # The connection's first settled request: the deposit goes back,
        # and this receipt says so.
        back = await _h2_finish_deposit(state, charge=False, reason="request_settled")
        if back is not None:
            receipt["deposit_released_msat"] = back["msat"]
    # Idle again before the client can see the receipt and send the next
    # request.
    _h2_request_done(state)
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
        extra={
            "sid": _short(state.sid),
            "model": billing.model_id,
            "cost_msats": cost,
            **log_extra,
        },
    )


async def _refuse_unattested(websocket: WebSocket, state: SessionState) -> None:
    """A usage disclosed without an attestation: release the reservation in
    full and tell the client the request was not settled."""
    billing = state.billing
    outcome: str | None = None
    state.h2_fault = state.h2_fault or "node_failure"
    async with state.lock:
        if state.finalized or billing is None:
            return
        try:
            await release(billing)
        except Exception:
            logger.exception(
                "confidential ws: could not release an unattested session",
                extra={"sid": _short(state.sid)},
            )
        else:
            state.finalized = True
            outcome = "released"
    _node_error(
        state,
        502,
        "usage without an attestation",
        {"error": {"type": "settlement_failed", "outcome": outcome}},
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
        elif state.upstream_refused:
            # The provider answered from the head alone and closed (a refusal
            # of the node's key or account, a rate limit): the session fee,
            # as for a disclosed refusal, never the abandon fee.
            cost, _ = await _release_keeping_fee(billing, "session_fee_msat")
            outcome = f"charged_session_fee:{cost}" if cost else "released"
        elif state.h2_fault in _H2_SIDECAR_FAULTS.values():
            # The provider could not be reached or the sidecar failed while
            # the request was open: not the client's doing, nothing is kept.
            await release(billing)
            outcome = "released"
        elif state.engaged and _fee(billing, "abandon_fee_msat") > 0:
            # π_WIN never verified, so the upstream never received a complete
            # request, but the sidecar ran the session (armed and never used,
            # aborted, client gone): the abandon fee is charged, the rest of
            # the reservation is released.
            cost, _ = await _release_keeping_fee(billing, "abandon_fee_msat")
            outcome = f"charged_abandon_fee:{cost}"
        else:
            # π_WIN never verified: the upstream never received a complete request.
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
# http: 2 sessions (protocol v3, option A)
# ---------------------------------------------------------------------------


def _h2_deposit_lifetime(idle_s: float) -> RequestLifetime:
    """The lifetime the deposit's reservation lease is derived from: the
    longest a connection can hold it (the setup phase, one idle period, a
    first request's two phases) plus the settlement grace."""
    seconds = max(
        float(settings.max_request_lifetime_seconds),
        2 * SESSION_TIMEOUT_S + idle_s + POST_RELEASE_TIMEOUT_S + SETTLE_GRACE_S,
    )
    return RequestLifetime(deadline=asyncio.get_running_loop().time() + seconds)


async def _open_h2_session(
    session_id: str, state: SessionState, msg: Any, side: dict[str, Any]
) -> Any:
    """The setup of an ``http: 2`` session: check the offer signature and the
    credential, reserve the connection deposit, dial the sidecar. Raises
    ``SetupError``; the deposit is then released (nothing was garbled)."""
    validate_h2_setup(msg)
    framing = msg["http"]
    if framing not in sidecar_framings(side):
        raise SetupError(400, f'this node does not serve framing "{framing}" now')
    block = h2_block(side) if framing == "2" else None
    auth = msg.get("auth", "") or ""
    # The offer the connection is opened on stays remembered while it lives;
    # its deposit and idle timeout are the connection's.
    terms = pin_offer(msg["offer_sig"])
    if terms is not None:
        state.offer_sig = msg["offer_sig"]
        state.offer_terms = terms
        state.h2_idle_timeout = float(terms.idle_timeout_s)
    state.framing = framing
    # The deposit's lease covers the connection up to its first settlement.
    request_lifetime.set(_h2_deposit_lifetime(state.h2_idle_timeout))
    try:
        await check_credential(auth, msg["offer_sig"])
        deposit = await reserve_deposit(auth, msg["offer_sig"])
    except BillingError as exc:
        raise SetupError(exc.status, str(exc), exc.detail) from exc
    except Exception as exc:
        logger.exception("confidential ws: credential check failed")
        raise SetupError(500, "credential check failed") from exc
    state.h2_deposit = deposit
    state.h2_deposit_msat = deposit.reserved_msats if deposit is not None else 0
    idle = block.get("idle_secs") if block is not None else None
    if _is_int(idle) and idle > 0:
        state.h2_idle_secs = float(idle)
    life = block.get("max_lifetime_s") if block is not None else None
    if _is_int(life) and life > 0:
        state.h2_max_lifetime = float(life)
    # The sidecar binds the sid to this value: the deposit's reservation id,
    # or a fresh random id when the offer asks for no deposit.
    escrow = (
        deposit.snapshot.release_id if deposit is not None else secrets.token_hex(16)
    )
    setup = {
        "type": "setup",
        "v": 3,
        "http": framing,
        "nonce_c": msg["nonce_c"],
        "armed": msg.get("armed", False),
        "escrow": f"conn-{escrow}",
    }
    try:
        _claim_slot(state)
        upstream = await _dial_sidecar(session_id, setup)
    except SetupError:
        # No slot, or the sidecar is unreachable: the node's side, and no
        # circuit was garbled.
        state.h2_setup_deposit = await _h2_finish_deposit(
            state, charge=False, reason="not_started"
        )
        raise
    if deposit is not None:
        logger.info(
            "confidential ws: connection deposit reserved",
            extra={"deposit_msat": state.h2_deposit_msat},
        )
    return upstream


def _h2_deposit_verdict(state: SessionState, ended: str) -> tuple[bool, str]:
    """Whether the deposit of a connection that is ending is charged, and why.

    Released: a request of the connection reached settlement or the provider
    (``request_settled``, ``request_received``, ``provider_refused``); the
    node, the sidecar or the provider failed before the client could get a
    request through (``state.h2_fault``); the sidecar never accepted the
    setup (``not_started``). Charged otherwise: the node garbled a key
    schedule for a connection that carried no request (``ended`` says how it
    ended: ``unused``, ``idle``, ``setup_timeout``, ``request_refused``), or
    whose only request was abandoned before the provider had it
    (``request_abandoned``)."""
    if state.h2_settled > 0:
        return False, "request_settled"
    if state.h2_fault is not None:
        return False, state.h2_fault
    if not state.h2_ready:
        return False, "not_started"
    if state.billing is not None:
        if state.finalized:
            # Finalized without a receipt: only a failed settlement does that.
            return False, "node_failure"
        if state.body_released:
            return False, "request_received"
        if state.upstream_refused:
            return False, "provider_refused"
        return True, "request_abandoned"
    return True, ended


async def _h2_finish_deposit(
    state: SessionState, charge: bool, reason: str
) -> dict[str, Any] | None:
    """Charge or release the connection deposit, once. Returns the outcome
    for the client (``{"msat", "outcome", "reason", …}``), or ``None`` when
    no deposit was held or it could not be finalized."""
    async with state.lock:
        deposit, state.h2_deposit = state.h2_deposit, None
        if deposit is None:
            return None
        try:
            if charge:
                balance = await charge_reservation(deposit)
            else:
                balance = await release(deposit)
        except Exception:
            # The reservation stays active; its lease ends with this task and
            # the stale-reservation sweeper releases it.
            logger.exception(
                "confidential ws: could not finalize the connection deposit",
                extra={"sid": _short(state.sid), "charge": charge},
            )
            return None
    outcome = "charged" if charge else "released"
    logger.info(
        f"confidential ws: connection deposit {outcome}",
        extra={
            "sid": _short(state.sid),
            "deposit_msat": deposit.reserved_msats,
            "reason": reason,
        },
    )
    return {
        "sid": state.sid,
        "msat": deposit.reserved_msats,
        "outcome": outcome,
        "reason": reason,
        "cost_msats": deposit.reserved_msats if charge else 0,
        "balance_msats": balance,
        "time": int(time.time()),
    }


def _with_deposit(frame: dict[str, Any], deposit: dict[str, Any] | None) -> str:
    """``frame`` (the connection's last: ``closed`` or ``error``) as text,
    carrying the deposit's outcome signed like a receipt."""
    if deposit is not None:
        frame = {
            **frame,
            "deposit": deposit,
            "deposit_json": canonical_json(deposit).decode("utf-8"),
            "deposit_sig": sign_payload(deposit),
            "sig_scheme": "schnorr-secp256k1",
        }
    return json.dumps(frame)


async def _h2_request_refused(state: SessionState) -> None:
    """The sidecar refused the request in flight before any of its records
    existed: release its reservation in full and free its number."""
    state.engaged = False
    await _finalize(state)
    state.billing = None
    state.body_released = False
    state.released_at = None
    state.upstream_refused = False
    state.finalized = False
    state.attestation_hash = ""
    state.h2_lifetime = None
    state.h2_n -= 1
    if state.h2_idle_since is not None:
        state.h2_idle_since = asyncio.get_running_loop().time()
    state.h2_moved.set()


def _h2_request_done(state: SessionState) -> None:
    """The request in flight is finalized with a receipt: idle again."""
    state.billing = None
    state.body_released = False
    state.released_at = None
    state.engaged = False
    state.upstream_refused = False
    state.finalized = False
    state.attestation_hash = ""
    state.h2_lifetime = None
    state.h2_settled += 1
    state.h2_idle_since = asyncio.get_running_loop().time()
    state.h2_moved.set()


def _h2_deadline(state: SessionState, started: float) -> float:
    """Loop time at which an ``http: 2`` session is cut off. With a request
    in flight, v2's limits counted from the request; before the connection
    head is written, v2's setup-phase limit; idle (no request in flight since
    ``head_written`` or the last settlement), the offer's ``idle_timeout_s``,
    which is never longer than the sidecar's own idle limit."""
    if state.h2_idle_since is None:
        # The connection head is not written yet (armed time included; in
        # framing "1.1" request 1 is already accepted).
        return started + SESSION_TIMEOUT_S
    if state.billing is not None:
        if state.released_at is None:
            return state.h2_request_at + SESSION_TIMEOUT_S
        deadline = state.released_at + POST_RELEASE_TIMEOUT_S
        if state.h2_lifetime is not None:
            deadline = min(deadline, state.h2_lifetime.deadline - SETTLE_GRACE_S)
        return deadline
    return state.h2_idle_since + min(state.h2_idle_timeout, state.h2_idle_secs)


async def _h2_reserve(msg: Any, state: SessionState) -> str:
    """Validate a ``request``, freeze its offer's rates and reserve, exactly
    as a v2 setup does. Returns the frame for the sidecar (no credential, no
    offer signature, no fees). Raises ``SetupError``; nothing is held then."""
    if state.billing is not None:
        raise ProtocolError(
            409, "a request is already in flight (requests are sequential)"
        )
    if state.framing == "1.1" and state.h2_n >= 1:
        raise ProtocolError(409, 'a framing "1.1" connection carries one request')
    if state.framing == "2" and state.h2_idle_since is None:
        raise ProtocolError(409, 'a framing "2" request comes after head_written')
    try:
        side = await cached_sidecar_offer()
        cap = int(side.get("max_tokens_cap", 4096))
    except Exception as exc:
        logger.warning("confidential ws: sidecar offer unavailable: %s", exc)
        raise SetupError(503, "confidential sidecar unavailable") from exc
    validate_h2_request(msg, cap, state.h2_n + 1)
    images = msg.get("kind") == "images"
    if images:
        served = images_endpoint(side) is not None
        if served and state.framing == "2":
            block = h2_block(side)
            served = block is not None and "images" in h2_kinds(block, True)
        if not served:
            raise SetupError(
                400, "this connection serves no confidential image requests"
            )
    if state.offer_terms is not None:
        try:
            check_request_offer(msg["offer_sig"], state.offer_terms, state.framing)
        except BillingError as exc:
            raise SetupError(exc.status, str(exc), exc.detail) from exc
    # The session fee pays for the handshake: due until the connection's
    # first settlement.
    fee_due = state.h2_settled == 0
    # The reservation's lease runs from this request, not from the connection.
    lifetime = _session_lifetime()
    request_lifetime.set(lifetime)
    try:
        if images:
            billing = await reserve_image(
                msg.get("auth", "") or "",
                msg["model"],
                msg["variant"],
                msg["count"],
                msg["offer_sig"],
                accepted_fee_msat=msg.get("session_fee_msat"),
                accepted_abandon_fee_msat=msg.get("abandon_fee_msat"),
                session_fee_due=fee_due,
            )
        else:
            billing = await reserve(
                msg.get("auth", "") or "",
                msg["model"],
                msg["max_tokens"],
                msg["offer_sig"],
                accepted_fee_msat=msg.get("session_fee_msat"),
                accepted_abandon_fee_msat=msg.get("abandon_fee_msat"),
                session_fee_due=fee_due,
            )
    except BillingError as exc:
        raise SetupError(exc.status, str(exc), exc.detail) from exc
    except Exception as exc:
        logger.exception("confidential ws: reservation failed")
        raise SetupError(500, "reservation failed") from exc
    state.billing = billing
    state.finalized = False
    state.body_released = False
    state.released_at = None
    state.engaged = False
    state.upstream_refused = False
    state.attestation_hash = ""
    state.h2_n = msg["n"]
    state.h2_request_at = asyncio.get_running_loop().time()
    state.h2_lifetime = lifetime
    logger.info(
        "confidential ws: request reserved",
        extra={
            "sid": _short(state.sid),
            "n": state.h2_n,
            "kind": getattr(billing, "kind", "chat"),
            "model": billing.model_id,
            "reserved_msats": billing.reserved_msats,
        },
    )
    if images:
        return json.dumps(
            {
                **{k: msg[k] for k in _H2_IMAGE_REQUEST_FIELDS if k in msg},
                "kind": "images",
                "count": billing.count,
                "suffix": billing.suffix,
            }
        )
    return json.dumps({k: msg[k] for k in _H2_REQUEST_FIELDS if k in msg})


async def _h2_client_to_sidecar(
    websocket: WebSocket, upstream: Any, state: SessionState
) -> None:
    """The client pump of an ``http: 2`` session: as v2's, except that a
    ``request`` frame goes to the session task (which reserves) and only its
    credential-free copy reaches the sidecar. Nothing later is forwarded
    until that is decided, so the request's records follow its ``request``."""
    loop = asyncio.get_running_loop()
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
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                msg = None
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "setup":
                await _send_error(websocket, 400, "duplicate setup")
                await _close(websocket, 1008, "duplicate setup")
                break
            if kind == "request":
                if len(text) > MAX_SETUP_TEXT:
                    await _send_error(websocket, 413, "request frame too large")
                    break
                reply: asyncio.Future[str | None] = loop.create_future()
                state.h2_requests.put_nowait((msg, reply))
                forward = await reply
                if forward is None:
                    break
                if forward:
                    await upstream.send(forward)
                # An empty string: the node refused the request and told the
                # client; the connection goes on.
                continue
            await upstream.send(text)
    except (WebSocketDisconnect, RuntimeError, websockets.ConnectionClosed):
        pass
    finally:
        await _close(upstream, 1000)


async def _run_h2_session(
    websocket: WebSocket, upstream: Any, state: SessionState, started: float
) -> None:
    """Relay an ``http: 2`` session. Reservations are made here, in the
    session's own task (the reservation's lease is renewed while its owner
    task lives), one per ``request``."""
    loop = asyncio.get_running_loop()
    client_pump = asyncio.create_task(_h2_client_to_sidecar(websocket, upstream, state))
    sidecar_pump = asyncio.create_task(
        _sidecar_to_client(websocket, upstream, state, close_client=False)
    )
    next_request: asyncio.Future[Any] | None = None
    moved: asyncio.Future[Any] | None = None
    cut = False
    # How a connection that carried no request ended (the deposit's reason),
    # and the node's own last frame, when it is the one ending the connection.
    ended = "unused"
    last: dict[str, Any] | None = None
    try:
        while True:
            if next_request is None:
                next_request = asyncio.ensure_future(state.h2_requests.get())
            if moved is None:
                moved = asyncio.ensure_future(state.h2_moved.wait())
            remaining = _h2_deadline(state, started) - loop.time()
            done, _ = await asyncio.wait(
                {client_pump, sidecar_pump, next_request, moved},
                timeout=max(remaining, 0.0),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if client_pump in done or sidecar_pump in done:
                if (
                    sidecar_pump in done
                    and client_pump not in done
                    and state.h2_sidecar_gone
                    and not state.h2_terminal
                    and state.h2_node_error is None
                ):
                    # The sidecar's socket ended with no ``error`` or
                    # ``closed`` while the client was still there. (A pump
                    # that ended because the client's socket did is the
                    # client leaving, not a failure of the sidecar.)
                    state.h2_fault = state.h2_fault or "sidecar_failure"
                    last = _error_frame(502, "confidential sidecar connection lost")
                    last["fault"] = "node"
                break
            if moved in done:
                # The deadline moved: wait again on the new one.
                state.h2_moved.clear()
                moved = None
                if state.h2_node_error is not None:
                    # The node's own failure: end the sidecar's session too.
                    last = state.h2_node_error
                    cut = True
                    break
                if state.h2_terminal:
                    # The sidecar's last word: the session is over.
                    break
            if next_request in done:
                msg, reply = next_request.result()
                next_request = None
                try:
                    forward = await _h2_reserve(msg, state)
                except SetupError as exc:
                    logger.warning("confidential ws: rejecting request: %s", exc)
                    if exc.status >= 500:
                        # The node's own failure: a connection that then ends
                        # without a settlement gets its deposit back.
                        state.h2_fault = state.h2_fault or "node_failure"
                    if exc.fatal:
                        last = _error_frame(exc.status, str(exc), exc.detail)
                        reply.set_result(None)
                        cut = True
                        break
                    # Refused before any of its records were sent (the client
                    # waits for ``request_ok``): nothing is held, the
                    # connection stays, and the request's number is free.
                    refused = {
                        "type": "request_refused",
                        "n": msg.get("n") if isinstance(msg, dict) else None,
                        "status": exc.status,
                        "reason": str(exc),
                    }
                    if exc.detail is not None:
                        refused["detail"] = exc.detail
                    try:
                        await websocket.send_text(json.dumps(refused))
                    except Exception:
                        reply.set_result(None)
                        break
                    if state.h2_idle_since is not None:
                        state.h2_idle_since = loop.time()
                    reply.set_result("")
                    continue
                reply.set_result(forward)
                continue
            if loop.time() >= _h2_deadline(state, started):
                if state.billing is None and state.h2_idle_since is not None:
                    # No request in flight for ``idle_timeout_s``: an orderly
                    # end. Nothing is held, except the deposit of a
                    # connection that never carried a request.
                    logger.info(
                        "confidential ws: connection idle, closing",
                        extra={"sid": _short(state.sid), "requests": state.h2_n},
                    )
                    ended = "idle"
                    last = {"type": "closed", "reason": "idle"}
                else:
                    logger.warning(
                        "confidential ws: session time limit reached",
                        extra={"sid": _short(state.sid), "n": state.h2_n},
                    )
                    ended = "setup_timeout"
                    last = _error_frame(408, "session time limit reached")
                cut = True
                break
        if cut:
            # As for v2's time limit: end the sidecar session, then hear its
            # last frames before the money decision.
            client_pump.cancel()
            await _drain_sidecar(upstream, sidecar_pump, SIDECAR_DRAIN_S)
        elif not state.h2_terminal:
            remaining = _h2_deadline(state, started) - loop.time()
            await asyncio.wait({sidecar_pump}, timeout=min(10.0, max(remaining, 0)))
    finally:
        for task in (client_pump, sidecar_pump, next_request, moved):
            if task is not None and not task.done():
                task.cancel()
        deposit = await _h2_end_deposit(state, ended)
        # The connection's last frame: the node's own, else the sidecar's
        # (``error`` or ``closed``), with the deposit's outcome.
        if last is None:
            last = state.h2_last_frame
        if last is None and deposit is not None:
            last = {"type": "closed", "reason": "ended"}
        if last is not None:
            try:
                await websocket.send_text(_with_deposit(last, deposit))
            except Exception:
                pass
        await _finalize(state)
        await _close(upstream, 1000)
        await _close(websocket, 1000)
        logger.info(
            "confidential ws: connection ended",
            extra={
                "sid": _short(state.sid),
                "http": state.framing,
                "requests": state.h2_n,
                "settled": state.h2_settled,
            },
        )


async def _h2_end_deposit(state: SessionState, ended: str) -> dict[str, Any] | None:
    """The connection is ending: charge or release a deposit still held
    (``_h2_deposit_verdict``) and return its outcome. When it is charged for
    a request the client abandoned, that request's reservation is released in
    full: the deposit is the price of the unused handshake, and the abandon
    fee would be the same price a second time."""
    if state.h2_deposit is None:
        return None
    charge, reason = _h2_deposit_verdict(state, ended)
    if charge and state.billing is not None:
        state.engaged = False
    return await _h2_finish_deposit(state, charge, reason)


# ---------------------------------------------------------------------------
# /zk
# ---------------------------------------------------------------------------


@confidential_ws_router.websocket("/v1/confidential/zk")
async def confidential_zk_proxy(
    websocket: WebSocket, proof: str = "", session_id: str = "", zk_token: str = ""
) -> None:
    global _active_zk
    await websocket.accept()
    if not _SID_RE.fullmatch(session_id or "") or not _PROOF_RE.fullmatch(proof or ""):
        await websocket.close(code=1008, reason="invalid session_id or proof")
        return
    # Only a session that passed setup (credential checked, reservation held)
    # on this node may open proof channels; anything else never reaches the
    # sidecar.
    state = _live_sids.get(session_id)
    if state is None or not hmac.compare_digest(
        zk_token.encode(), state.zk_token.encode()
    ):
        await websocket.close(code=1008, reason="unknown session")
        return
    if state.zk_open >= MAX_ZK_CONNS_PER_SESSION or _active_zk >= MAX_ZK_CONNS:
        await _close(websocket, 1013, "too many proof connections")
        return
    _active_zk += 1
    state.zk_open += 1
    try:
        cap = _ZK_PROXY_CAP_S + state.h2_max_lifetime
        await _zk_proxy(websocket, proof, session_id, state.ended, cap)
    finally:
        _active_zk -= 1
        state.zk_open -= 1


async def _zk_proxy(
    websocket: WebSocket,
    proof: str,
    session_id: str,
    ended: asyncio.Event,
    cap: float = _ZK_PROXY_CAP_S,
) -> None:
    try:
        # No keepalive pings on this loopback hop: while the client's link is
        # slower than the sidecar's (the 2PC sends tens of MB), this proxy stops
        # reading from the sidecar, its pongs queue unread and a ping timeout
        # would kill a healthy session.
        upstream = await websockets.connect(
            _sidecar_ws_url("/zk", f"proof={proof}&sid={session_id}"),
            extra_headers=sidecar_headers(),
            max_size=_WS_MAX_SIZE,
            open_timeout=10,
            ping_interval=None,
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
    # A pre-warmed session keeps its 2PC and proof channels open until it is
    # claimed, and π_C3S runs after the response: a channel lives as long as
    # its session. When the session ends (settled, released or timed out) its
    # proxies are closed too; the cap is only a backstop past both phases.
    both = asyncio.create_task(asyncio.wait(tasks))
    session_end = asyncio.create_task(ended.wait())
    await asyncio.wait(
        {both, session_end},
        timeout=cap,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in (*tasks, both, session_end):
        if not task.done():
            task.cancel()
    await _close(upstream, 1000)
    await _close(websocket, 1000)


__all__ = ["confidential_ws_router"]
