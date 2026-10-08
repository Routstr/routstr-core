"""Channel-B WebSocket proxy: expose proverd's TLSN mux (``GET /ws``) on the
node's own public URL so remote verifiers can reach it.

``GET /v1/tlsn/ws?session_id=<uuid>`` accepts a client websocket and proxies
frames bidirectionally to proverd's ``GET /ws?session_id=<uuid>`` (proverd
base from ``settings.tlsn_proverd_url``). Binary passthrough; close semantics
propagate both ways. The router is only registered when
``settings.tlsn_proverd_url`` is set (see ``routstr/core/main.py``).

DoS note (accepted for now, see TLSN-routstr.md): unauthenticated connects
with unknown session ids occupy a proverd pairing slot until its 30s pairing
timeout reaps them.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .core import get_logger
from .core.settings import settings

logger = get_logger(__name__)

tlsn_ws_router = APIRouter()

# Session ids are uuids minted by the SDK verifier.
_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Proof data flows over this mux; keep frame sizes comfortably above the
# max transcript proverd allows (1 MiB recv, see upstream/tlsn_verified.py).
_WS_MAX_SIZE = 16 << 20


def _proverd_ws_url(session_id: str) -> str:
    return (
        settings.tlsn_proverd_url.replace("http", "ws", 1).rstrip("/")
        + f"/ws?session_id={session_id}"
    )


def tlsn_proverd_ws_public_url() -> str:
    """Channel-B endpoint advertised in ``/v1/models`` (``tlsn.proverd_ws``).

    Remote verifiers cannot reach proverd's loopback/compose-network address,
    so when the node knows its public URL (``HTTP_URL``) advertise the node's
    own ws proxy route above. Falls back to the direct proverd address for
    local dev, where verifier and node share a host.
    """
    public = (settings.http_url or "").rstrip("/")
    if public:
        scheme, _, rest = public.partition("://")
        ws_scheme = "wss" if scheme == "https" else "ws"
        return f"{ws_scheme}://{rest}/v1/tlsn/ws"
    return _proverd_ws_url("").split("?")[0]


async def _pump_client_to_upstream(
    websocket: WebSocket, upstream: Any, session_id: str
) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                await upstream.send(data)
            else:
                text = message.get("text")
                if text is not None:
                    await upstream.send(text)
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # starlette raises when receive() races an already-handled disconnect
        pass
    except websockets.ConnectionClosed as exc:
        logger.info(
            "tlsn ws proxy: proverd closed while client still sending",
            extra={"session_id": session_id, "code": exc.code},
        )
    finally:
        try:
            await upstream.close()
        except Exception:
            pass


async def _pump_upstream_to_client(
    websocket: WebSocket, upstream: Any, session_id: str
) -> None:
    try:
        async for frame in upstream:
            if isinstance(frame, bytes):
                await websocket.send_bytes(frame)
            else:
                await websocket.send_text(frame)
    except websockets.ConnectionClosed:
        pass
    except RuntimeError:
        # client socket already gone
        pass
    except Exception as exc:
        logger.warning(
            "tlsn ws proxy: error forwarding proverd frame",
            extra={"session_id": session_id, "error": f"{type(exc).__name__}: {exc}"},
        )
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@tlsn_ws_router.websocket("/v1/tlsn/ws")
async def tlsn_ws_proxy(websocket: WebSocket, session_id: str = "") -> None:
    """Bidirectional byte proxy between a remote verifier and proverd's mux."""
    # Accept-then-close so clients get proper ws close frames, not HTTP errors.
    await websocket.accept()

    if not _SESSION_ID_RE.match(session_id or ""):
        await websocket.close(code=1008, reason="invalid session_id")
        return

    upstream_url = _proverd_ws_url(session_id)
    try:
        upstream = await websockets.connect(
            upstream_url,
            max_size=_WS_MAX_SIZE,
            open_timeout=10,
        )
    except Exception as exc:
        logger.warning(
            "tlsn ws proxy: proverd unreachable",
            extra={"session_id": session_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        await websocket.close(code=1011, reason="proverd unreachable")
        return

    logger.info(
        "tlsn ws proxy: session paired",
        extra={"session_id": session_id, "upstream": upstream_url.split("?")[0]},
    )

    try:
        client_pump = asyncio.create_task(
            _pump_client_to_upstream(websocket, upstream, session_id)
        )
        upstream_pump = asyncio.create_task(
            _pump_upstream_to_client(websocket, upstream, session_id)
        )
        done, pending = await asyncio.wait(
            {client_pump, upstream_pump}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Surface unexpected pump errors (close propagation is already done).
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "tlsn ws proxy: pump ended with error",
                    extra={
                        "session_id": session_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
    finally:
        try:
            await upstream.close()
        except Exception:
            pass

    logger.info("tlsn ws proxy: session closed", extra={"session_id": session_id})
