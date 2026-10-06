"""TLSN verified mode: forward upstream calls through proverd (TLSNotary
Proxy-TLS prover sidecar) instead of a direct httpx connection.

Contract (see TLSN-routstr.md + STATUS.md architect notes):
- Opt-in per request via the ``x-routstr-verify: tlsn-proxy`` header.
- The SDK generates a session id and sends it as ``x-routstr-tlsn-session``;
  the same id pairs the verifier's websocket to proverd's ``POST /sessions``.
- Response body is byte-passthrough (the SDK compares it against the proven
  TLS transcript). No ``usage.cost`` injection, no model rewriting; cost data
  goes only into the ``x-routstr-cost-*`` headers.
- Verified mode forces ``accept-encoding: identity`` upstream so wire bytes
  equal transcript bytes (httpx would transparently decompress otherwise).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from ..auth import ReservationSnapshot, adjust_payment_for_tokens
from ..core import get_logger
from ..core.db import ApiKey, create_session
from ..core.exceptions import UpstreamError
from ..core.settings import settings
from ..payment.models import Model
from .base import (
    _inject_cost_response_headers,
)
from .count_tokens import MissingUsageEstimator
from .stream_ownership import (
    ClosingStreamingResponse,
    PersistentStreamFinalizer,
)

if TYPE_CHECKING:
    from .base import BaseUpstreamProvider

logger = get_logger(__name__)

VERIFY_HEADER = "x-routstr-verify"
SESSION_HEADER = "x-routstr-tlsn-session"
VERIFY_MODE_PROXY = "tlsn-proxy"

# Credential-bearing headers whose VALUES proverd must keep hidden in the
# proof. Everything else in the transcript is disclosed to the verifier.
_REDACTABLE_HEADERS = {"authorization", "x-api-key", "api-key", "proxy-authorization"}

# Hop-by-hop / framing headers proverd strips or that must not be copied from
# the proverd response onto the client response.
_DROP_RESPONSE_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "content-encoding",
    "x-tlsn-session",
    "x-tlsn-prover",
}

# Headers forwarded from the (proven) upstream response to the client.
_ALLOWED_RESPONSE_HEADERS = {
    "content-type",
    "cache-control",
    "date",
    "vary",
    "access-control-allow-origin",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-allow-credentials",
    "access-control-expose-headers",
    "access-control-max-age",
    "openai-organization",
    "openai-processing-ms",
    "openai-version",
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
}


def verified_mode_requested(request: Request) -> bool:
    """True if the client asked for TLSN proxy-mode verification."""
    return request.headers.get(VERIFY_HEADER, "").strip().lower() == VERIFY_MODE_PROXY


def require_verified_mode_available(request: Request) -> str:
    """Validate the verified-mode preconditions. Returns the session id.

    Fails loud (400) when verified mode is requested but unavailable or the
    request is malformed — never silently serve an unverified response.
    """
    if not settings.tlsn_proverd_url:
        raise UpstreamError(
            "tlsn verified mode requested but this node has no proverd "
            "configured (TLSN_PROVERD_URL unset)",
            status_code=400,
        )
    session_id = request.headers.get(SESSION_HEADER, "").strip()
    if not session_id:
        raise UpstreamError(
            f"tlsn verified mode requires the {SESSION_HEADER} header",
            status_code=400,
        )
    return session_id


def _proverd_client() -> httpx.AsyncClient:
    # Verified sessions can run long (relay + proof); generous read timeout.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
    )


def _build_proverd_payload(
    *,
    provider: "BaseUpstreamProvider",
    request: Request,
    path: str,
    headers: dict[str, str],
    body: bytes | None,
    url: str,
    session_id: str,
) -> dict[str, Any]:
    parsed = urlsplit(url)
    server_name = parsed.hostname
    if not server_name:
        raise UpstreamError(
            f"cannot determine upstream host from {url!r} for verified mode",
            status_code=500,
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    params = provider.prepare_params(path, request.query_params)
    query = urlencode(params) if params else ""
    upstream_path = parsed.path + ("?" + query if query else "")

    # Byte-equality requires the transcript bytes to be exactly what the
    # client receives: no compression anywhere on the path.
    out_headers = dict(headers)
    out_headers["accept-encoding"] = "identity"

    redact = sorted(
        name.lower() for name in out_headers if name.lower() in _REDACTABLE_HEADERS
    )

    try:
        body_text = body.decode("utf-8") if body else ""
    except UnicodeDecodeError:
        raise UpstreamError(
            "tlsn verified mode does not support binary request bodies yet",
            status_code=400,
        )

    return {
        "session_id": session_id,
        "server_name": server_name,
        "port": port,
        "request": {
            "method": request.method,
            "path": upstream_path,
            "headers": [[k, v] for k, v in out_headers.items()],
            "body": body_text,
        },
        "redact": redact,
        # Design doc decision 6: cap transcript sizes; verified mode returns
        # unavailable rather than failing mid-stream.
        "max_recv_bytes": 1 << 20,
    }


def _verified_response_headers(
    proverd_headers: httpx.Headers,
    *,
    session_id: str,
    server_name: str,
) -> dict[str, str]:
    out = {
        k: v
        for k, v in proverd_headers.items()
        if k.lower() in _ALLOWED_RESPONSE_HEADERS
        and k.lower() not in _DROP_RESPONSE_HEADERS
    }
    out["x-routstr-verified"] = VERIFY_MODE_PROXY
    out["x-routstr-upstream-host"] = server_name
    out["x-routstr-tlsn-session"] = session_id
    return out


async def forward_verified_via_proverd(
    *,
    provider: "BaseUpstreamProvider",
    request: Request,
    path: str,
    headers: dict[str, str],
    request_body: bytes | None,
    key: ApiKey,
    max_cost_for_model: int,
    session: Any,
    model_obj: Model,
    reservation_snapshot: ReservationSnapshot | None,
    url: str,
    original_model_id: str | None,
) -> Response | StreamingResponse:
    """Verified-mode replacement for the direct upstream call.

    Non-streaming only for now: byte-passthrough body, cost in headers, no
    body mutation of any kind.
    """
    session_id = require_verified_mode_available(request)

    wants_stream = False
    if request_body:
        try:
            wants_stream = bool(json.loads(request_body).get("stream", False))
        except (json.JSONDecodeError, AttributeError):
            pass

    payload = _build_proverd_payload(
        provider=provider,
        request=request,
        path=path,
        headers=headers,
        body=request_body,
        url=url,
        session_id=session_id,
    )
    server_name = payload["server_name"]
    proverd_url = settings.tlsn_proverd_url.rstrip("/")

    if wants_stream:
        return await _forward_verified_stream(
            provider=provider,
            request=request,
            path=path,
            payload=payload,
            proverd_url=proverd_url,
            request_body=request_body,
            key=key,
            max_cost_for_model=max_cost_for_model,
            model_obj=model_obj,
            reservation_snapshot=reservation_snapshot,
            session_id=session_id,
            server_name=server_name,
            original_model_id=original_model_id,
        )

    logger.info(
        "Forwarding via proverd (verified mode)",
        extra={
            "session_id": session_id,
            "server_name": server_name,
            "path": path,
            "model": original_model_id or "unknown",
            "key_hash": key.hashed_key[:8] + "...",
        },
    )

    try:
        async with _proverd_client() as client:
            proverd_resp = await client.send(
                client.build_request("POST", f"{proverd_url}/sessions", json=payload),
                stream=True,
            )
            try:
                # Read raw bytes: httpx would transparently decompress a
                # content-encoded body, breaking byte-equality with the proof.
                body = b"".join([chunk async for chunk in proverd_resp.aiter_raw()])
            finally:
                await proverd_resp.aclose()
    except httpx.RequestError as exc:
        raise UpstreamError(
            f"proverd unreachable or session failed: {type(exc).__name__}",
            status_code=502,
        )

    if proverd_resp.status_code != 200:
        # proverd mirrors the upstream status; map like a normal upstream
        # error so billing revert and error shape stay consistent.
        upstream_error = httpx.Response(
            status_code=proverd_resp.status_code,
            headers=[
                (k, v)
                for k, v in proverd_resp.headers.items()
                if k.lower() not in _DROP_RESPONSE_HEADERS
            ],
            content=body,
            request=httpx.Request(request.method, url),
        )
        return await provider.forward_upstream_error_response(
            request, path, upstream_error, model_id=original_model_id
        )

    # --- non-streaming: byte-passthrough + header-only cost metadata ---
    response_json: dict[str, Any]
    try:
        parsed_json = json.loads(body)
        response_json = parsed_json if isinstance(parsed_json, dict) else {}
    except json.JSONDecodeError:
        response_json = {}

    if response_json:
        usage = response_json.get("usage")
        if not isinstance(usage, dict) or not usage:
            usage_estimator = MissingUsageEstimator(request_body, model_obj)
            usage_estimator.observe(response_json)
            response_json["usage"] = usage_estimator.openai_response_data(
                response_json.get("model")
            )["usage"]

    cost_data = await adjust_payment_for_tokens(
        key,
        response_json,
        session,
        max_cost_for_model,
        model_obj,
        provider.provider_fee,
        reservation_snapshot,
    )

    response_headers = _verified_response_headers(
        proverd_resp.headers, session_id=session_id, server_name=server_name
    )
    _inject_cost_response_headers(response_headers, cost_data)

    logger.info(
        "Verified (non-streaming) response served",
        extra={
            "session_id": session_id,
            "server_name": server_name,
            "bytes": len(body),
            "cost_msats": cost_data.get("total_msats"),
            "key_hash": key.hashed_key[:8] + "...",
        },
    )

    return Response(
        content=body,
        status_code=proverd_resp.status_code,
        headers=response_headers,
        media_type=proverd_resp.headers.get("content-type"),
    )


async def _forward_verified_stream(
    *,
    provider: "BaseUpstreamProvider",
    request: Request,
    path: str,
    payload: dict[str, Any],
    proverd_url: str,
    request_body: bytes | None,
    key: ApiKey,
    max_cost_for_model: int,
    model_obj: Model,
    reservation_snapshot: ReservationSnapshot | None,
    session_id: str,
    server_name: str,
    original_model_id: str | None,
) -> Response | StreamingResponse:
    """Verified SSE: stream upstream bytes untouched, settle billing after
    ``data: [DONE]`` (or disconnect) from the captured usage chunk.

    Cost metadata cannot ride in headers (they precede the stream), so the
    settlement is silent — the client observes the balance delta on its next
    request. The proof itself arrives on the verifier's mux channel.
    """
    # The client must outlive this function: the StreamingResponse body is
    # consumed after the endpoint returns. Closed by the finalizer.
    client = _proverd_client()
    try:
        proverd_resp = await client.send(
            client.build_request("POST", f"{proverd_url}/sessions", json=payload),
            stream=True,
        )
    except httpx.RequestError as exc:
        await client.aclose()
        raise UpstreamError(
            f"proverd unreachable or session failed: {type(exc).__name__}",
            status_code=502,
        )

    if proverd_resp.status_code != 200:
        try:
            error_body = b"".join([c async for c in proverd_resp.aiter_raw()])
        finally:
            await proverd_resp.aclose()
            await client.aclose()
        upstream_error = httpx.Response(
            status_code=proverd_resp.status_code,
            headers=[
                (k, v)
                for k, v in proverd_resp.headers.items()
                if k.lower() not in _DROP_RESPONSE_HEADERS
            ],
            content=error_body,
            request=httpx.Request(request.method, path),
        )
        return await provider.forward_upstream_error_response(
            request, path, upstream_error, model_id=original_model_id
        )

    usage_estimator = MissingUsageEstimator(request_body, model_obj)
    usage_finalized = False
    seen_usage: dict[str, Any] | None = None
    last_model_seen: str | None = None

    def observe_event(raw_event: bytes) -> None:
        nonlocal seen_usage, last_model_seen
        for line in raw_event.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            usage_estimator.observe(parsed)
            model = parsed.get("model")
            if isinstance(model, str):
                last_model_seen = model
            usage = parsed.get("usage")
            if isinstance(usage, dict) and usage:
                seen_usage = usage

    async def settle_billing() -> None:
        nonlocal usage_finalized
        if usage_finalized:
            return
        usage_finalized = True
        try:
            async with create_session() as new_session:
                fresh_key = await new_session.get(key.__class__, key.hashed_key)
                if not fresh_key:
                    return
                if seen_usage:
                    payload_json: dict[str, Any] = {
                        "usage": seen_usage,
                        "model": last_model_seen or original_model_id,
                    }
                else:
                    logger.warning(
                        "Verified stream carried no usage chunk; billing from "
                        "local token estimate",
                        extra={"session_id": session_id},
                    )
                    payload_json = usage_estimator.response_data(
                        last_model_seen or original_model_id
                    )
                await adjust_payment_for_tokens(
                    fresh_key,
                    payload_json,
                    new_session,
                    max_cost_for_model,
                    model_obj,
                    provider.provider_fee,
                    reservation_snapshot,
                )
        except Exception:
            logger.exception(
                "Verified stream billing settlement failed",
                extra={"session_id": session_id},
            )

    async def finalize() -> None:
        try:
            await settle_billing()
        finally:
            await proverd_resp.aclose()
            await client.aclose()

    finalizer = PersistentStreamFinalizer(finalize)

    async def passthrough() -> Any:
        pending = b""
        try:
            async for chunk in proverd_resp.aiter_raw():
                pending += chunk
                while b"\n\n" in pending:
                    event, pending = pending.split(b"\n\n", 1)
                    observe_event(event)
                yield chunk
            if pending:
                observe_event(pending)
        finally:
            await finalizer.run()

    logger.info(
        "Streaming verified response (SSE passthrough)",
        extra={"session_id": session_id, "server_name": server_name},
    )

    response_headers = _verified_response_headers(
        proverd_resp.headers, session_id=session_id, server_name=server_name
    )
    return ClosingStreamingResponse(
        passthrough(),
        finalizer=finalizer,
        status_code=200,
        headers=response_headers,
        media_type=proverd_resp.headers.get("content-type", "text/event-stream"),
    )
