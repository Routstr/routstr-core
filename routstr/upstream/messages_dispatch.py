"""Pure helpers for translating ``/v1/messages`` to upstream chat completions
via litellm.

This module owns the litellm/Anthropic-Messages translation layer:

* SSE parsing (``parse_sse_blocks``, ``events_from_chunk``)
* Payload coercion (``coerce_litellm_payload``)
* Stream aggregation (``aggregate_anthropic_events_to_message``) — drains
  a streamed Anthropic event sequence into a single Message dict
* Per-event annotation for streaming (``annotate_event``,
  ``stream_annotated_events``) — handles the model-rewrite + token-tally
  bookkeeping shared by the bearer-key and x-cashu streaming paths
* The dispatch entry point (``dispatch_anthropic_messages``)
* Refund math (``compute_refund``)

Nothing in here touches ``BaseUpstreamProvider``; the thin instance methods
on the provider class forward to these functions and only retain logic that
genuinely needs ``self`` (cost adjustment, metadata injection, refund
sending).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, Callable, NamedTuple, cast

import litellm

from ..core import get_logger
from ..core.exceptions import UpstreamError
from ..payment.models import Model

logger = get_logger(__name__)

# Anthropic-Messages-only fields that don't translate to OpenAI
# Chat Completions. ``litellm.drop_params`` only filters *known*
# unsupported params; these newer/extension fields get passed through
# verbatim and the upstream rejects them with a 400. Pop them here so the
# request reaches the upstream cleanly.
ANTHROPIC_ONLY_FIELDS: tuple[str, ...] = (
    "thinking",
    "cache_control",
    "context_management",
    "output_config",
    "mcp_servers",
    "service_tier",
    "anthropic_version",
    "anthropic_beta",
)


def coerce_litellm_payload(payload: object) -> dict:
    """Convert a litellm event into a plain dict.

    Non-streaming responses come back as Anthropic-shaped pydantic models
    or dicts. Streaming may yield raw bytes/str (SSE-encoded); those go
    through ``events_from_chunk`` instead, not here.
    """
    if isinstance(payload, dict):
        return dict(payload)
    if hasattr(payload, "model_dump"):
        return cast(dict, payload.model_dump())
    raise TypeError(f"Cannot coerce {type(payload).__name__} to dict")


def parse_sse_blocks(buffer: bytes) -> tuple[list[dict], bytes]:
    """Parse complete SSE event blocks out of a byte buffer.

    Returns (events, remaining_buffer). Events are JSON objects parsed from
    one or more ``data:`` lines per block. Comments, blank lines, and
    ``[DONE]`` sentinels are ignored. A trailing partial block is preserved
    in remaining_buffer.
    """
    events: list[dict] = []
    while True:
        sep = buffer.find(b"\n\n")
        if sep < 0:
            sep_rn = buffer.find(b"\r\n\r\n")
            if sep_rn < 0:
                break
            block = buffer[:sep_rn]
            buffer = buffer[sep_rn + 4 :]
        else:
            block = buffer[:sep]
            buffer = buffer[sep + 2 :]

        data_lines: list[str] = []
        for raw_line in block.replace(b"\r\n", b"\n").split(b"\n"):
            line = raw_line.decode("utf-8", errors="replace")
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, buffer


def events_from_chunk(
    chunk: object, sse_buffer: bytes
) -> tuple[list[dict], bytes]:
    """Normalize a stream chunk into one or more event dicts.

    ``litellm.anthropic.messages.acreate(stream=True)`` yields raw SSE
    bytes in practice; some adapters yield strings or typed events. Handle
    all three.
    """
    if isinstance(chunk, (bytes, bytearray)):
        sse_buffer += bytes(chunk)
        events, sse_buffer = parse_sse_blocks(sse_buffer)
        return events, sse_buffer
    if isinstance(chunk, str):
        sse_buffer += chunk.encode("utf-8")
        events, sse_buffer = parse_sse_blocks(sse_buffer)
        return events, sse_buffer
    return [coerce_litellm_payload(chunk)], sse_buffer


async def aggregate_anthropic_events_to_message(
    iterator: AsyncIterator[Any],
) -> dict:
    """Drain an Anthropic-Messages event iterator into a single Message dict.

    Produces the shape ``litellm.anthropic.messages.acreate(stream=False)``
    would have returned. Used to transparently stream from upstream while
    still returning a non-streaming response to the client. Lets us
    sidestep upstream quirks (e.g. Fireworks rejects ``max_tokens > 4096``
    unless ``stream=true``) without leaking that into client-visible
    behavior.
    """
    sse_buffer = b""
    message: dict = {}
    blocks: list[dict] = []
    partial_json: dict[int, str] = {}
    final_stop_reason: str | None = None
    final_stop_sequence: str | None = None
    final_usage: dict[str, Any] = {}
    final_model: str | None = None

    async for chunk in iterator:
        events, sse_buffer = events_from_chunk(chunk, sse_buffer)
        for event in events:
            etype = event.get("type")
            if etype == "message_start":
                raw = event.get("message") or {}
                if isinstance(raw, dict):
                    message = dict(raw)
                    existing = message.get("content")
                    blocks = list(existing) if isinstance(existing, list) else []
                    usage = message.get("usage")
                    if isinstance(usage, dict):
                        final_usage = dict(usage)
                    if isinstance(message.get("model"), str):
                        final_model = message["model"]
            elif etype == "content_block_start":
                idx = int(event.get("index") or 0)
                cb = event.get("content_block") or {}
                cb_dict = dict(cb) if isinstance(cb, dict) else {}
                while len(blocks) <= idx:
                    blocks.append({})
                blocks[idx] = cb_dict
            elif etype == "content_block_delta":
                idx = int(event.get("index") or 0)
                if idx >= len(blocks):
                    continue
                delta = event.get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                dtype = delta.get("type")
                block = blocks[idx]
                if dtype == "text_delta":
                    block["text"] = (block.get("text") or "") + (
                        delta.get("text") or ""
                    )
                elif dtype == "input_json_delta":
                    partial_json[idx] = partial_json.get(idx, "") + (
                        delta.get("partial_json") or ""
                    )
                elif dtype == "thinking_delta":
                    block["thinking"] = (block.get("thinking") or "") + (
                        delta.get("thinking") or ""
                    )
                elif dtype == "signature_delta":
                    block["signature"] = (block.get("signature") or "") + (
                        delta.get("signature") or ""
                    )
            elif etype == "content_block_stop":
                idx = int(event.get("index") or 0)
                raw_json = partial_json.pop(idx, None)
                if raw_json is not None and idx < len(blocks):
                    try:
                        blocks[idx]["input"] = (
                            json.loads(raw_json) if raw_json else {}
                        )
                    except json.JSONDecodeError:
                        blocks[idx]["input"] = raw_json
            elif etype == "message_delta":
                delta = event.get("delta") or {}
                if isinstance(delta, dict):
                    if "stop_reason" in delta:
                        final_stop_reason = delta.get("stop_reason")
                    if "stop_sequence" in delta:
                        final_stop_sequence = delta.get("stop_sequence")
                usage = event.get("usage")
                if isinstance(usage, dict):
                    final_usage.update(usage)
            # message_stop: nothing to merge

    if not message:
        # Upstream returned no message_start; expose what we can so the
        # client at least sees the assembled content.
        message = {
            "id": "",
            "type": "message",
            "role": "assistant",
            "content": [],
        }

    message["content"] = blocks
    if final_model and not message.get("model"):
        message["model"] = final_model
    if final_stop_reason is not None:
        message["stop_reason"] = final_stop_reason
    if final_stop_sequence is not None:
        message["stop_sequence"] = final_stop_sequence
    if final_usage:
        existing_usage = message.get("usage")
        merged = dict(existing_usage) if isinstance(existing_usage, dict) else {}
        merged.update(final_usage)
        message["usage"] = merged
    return message


class AnnotatedEvent(NamedTuple):
    """One Anthropic SSE event after model-rewrite + token-tally bookkeeping.

    ``sse_bytes`` is the wire-ready ``event:`` / ``data:`` block; the two
    streaming paths in ``BaseUpstreamProvider`` consume ``sse_bytes`` plus
    the tallies and only differ in whether they stream live or buffer
    first.

    ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` are
    surfaced separately so the cost path can price them against the cache
    rate rather than fold them silently into the regular input bucket.

    ``total_cost`` / ``input_cost`` / ``output_cost`` carry any
    USD cost figures the upstream attached to this event (from
    ``usage.cost``, ``usage.total_cost``, or ``usage.cost_details``) so the
    streaming paths can re-embed them in the rebuilt ``usage`` dict and let
    ``calculate_cost`` convert directly USD→sats instead of falling back to
    token-based math.
    """

    event: dict
    sse_bytes: bytes
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    total_cost: float
    input_cost: float
    output_cost: float
    model: str | None


def annotate_event(event: dict, requested_model: str | None) -> AnnotatedEvent:
    """Rewrite ``model`` fields and extract per-event token / model info.

    Mutates ``event`` in place when ``requested_model`` is set so the
    upstream's true model name doesn't leak to the client.
    """
    if requested_model:
        msg = event.get("message")
        if isinstance(msg, dict) and "model" in msg:
            msg["model"] = requested_model
        if "model" in event:
            event["model"] = requested_model

    in_tokens = 0
    out_tokens = 0
    cache_read_tokens = 0
    cache_create_tokens = 0
    total_cost = 0.0
    input_cost = 0.0
    output_cost = 0.0
    model: str | None = None

    def _coerce_float(value: object) -> float:
        if value is None or isinstance(value, bool):
            return 0.0
        if not isinstance(value, (int, float, str)):
            return 0.0
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0

    def _accumulate(usage: dict) -> None:
        nonlocal in_tokens, out_tokens, cache_read_tokens, cache_create_tokens
        nonlocal total_cost, input_cost, output_cost
        in_tokens += int(usage.get("input_tokens") or 0)
        out_tokens += int(usage.get("output_tokens") or 0)
        cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
        cache_create_tokens += int(usage.get("cache_creation_input_tokens") or 0)
        total_cost += _coerce_float(usage.get("total_cost"))
        input_cost += _coerce_float(usage.get("input_cost"))
        output_cost += _coerce_float(usage.get("output_cost"))

    msg_for_meta = event.get("message")
    if isinstance(msg_for_meta, dict):
        if msg_for_meta.get("model"):
            model = str(msg_for_meta["model"])
        usage = msg_for_meta.get("usage")
        if isinstance(usage, dict):
            _accumulate(usage)

    if isinstance(event.get("usage"), dict):
        _accumulate(event["usage"])

    # Some upstreams (notably OpenRouter-style proxies) attach cost fields
    # directly at the event root rather than inside ``usage``.
    for field in ("total_cost", "cost"):
        total_cost = max(total_cost, _coerce_float(event.get(field)))
    input_cost = max(input_cost, _coerce_float(event.get("input_cost")))
    output_cost = max(output_cost, _coerce_float(event.get("output_cost")))
    root_cost_details = event.get("cost_details")
    if isinstance(root_cost_details, dict):
        total_cost = max(
            total_cost,
            _coerce_float(root_cost_details.get("total_cost")),
        )
        input_cost = max(
            input_cost,
            _coerce_float(root_cost_details.get("input_cost")),
        )
        output_cost = max(
            output_cost,
            _coerce_float(root_cost_details.get("output_cost")),
        )

    event_type = str(event.get("type") or "")
    payload = json.dumps(event)
    if event_type:
        sse_bytes = f"event: {event_type}\ndata: {payload}\n\n".encode()
    else:
        sse_bytes = f"data: {payload}\n\n".encode()

    return AnnotatedEvent(
        event,
        sse_bytes,
        in_tokens,
        out_tokens,
        cache_read_tokens,
        cache_create_tokens,
        total_cost,
        input_cost,
        output_cost,
        model,
    )


async def stream_annotated_events(
    iterator: AsyncIterator[Any],
    requested_model: str | None,
) -> AsyncGenerator[AnnotatedEvent, None]:
    """Yield annotated, SSE-serialized events from a litellm stream.

    Both streaming paths in ``BaseUpstreamProvider`` consume this; the only
    divergence between them — yield-as-you-go vs buffer-then-replay — stays
    in the caller.
    """
    sse_buffer = b""
    async for chunk in iterator:
        events, sse_buffer = events_from_chunk(chunk, sse_buffer)
        for event in events:
            yield annotate_event(event, requested_model)


def embed_usd_costs(
    usage: dict,
    total_cost: float,
    input_cost: float,
    output_cost: float,
) -> None:
    """Mutate ``usage`` so ``calculate_cost`` will pick up the USD totals.

    Mirrors the upstream shape: when any USD figure is present, attach
    ``cost`` (used by the simple-fallback branch in ``calculate_cost``) and
    a ``cost_details`` block (used by the preferred branch — also gives the
    input/output USD split when we have one).
    """
    if total_cost <= 0 and input_cost <= 0 and output_cost <= 0:
        return

    cost_details: dict[str, float] = {}
    effective_total = total_cost
    if effective_total <= 0 and (input_cost > 0 or output_cost > 0):
        effective_total = input_cost + output_cost

    if effective_total > 0:
        cost_details["total_cost"] = effective_total
        usage["cost"] = effective_total
    if input_cost > 0:
        cost_details["input_cost"] = input_cost
    if output_cost > 0:
        cost_details["output_cost"] = output_cost
    if cost_details:
        usage["cost_details"] = cost_details


def compute_refund(amount: int, unit: str, cost_msats: int) -> int:
    if unit == "msat":
        return amount - cost_msats
    if unit == "sat":
        return amount - (cost_msats + 999) // 1000
    raise ValueError(f"Invalid unit: {unit}")


async def dispatch_anthropic_messages(
    *,
    request_body: bytes | None,
    model_obj: Model,
    base_url: str,
    api_key: str,
    provider_prefix: str,
    transform_model_name: Callable[[str], str],
    log_extra: dict[str, Any] | None = None,
) -> tuple[bool, Any, str | None]:
    """Call ``litellm.anthropic.messages.acreate`` and return
    ``(client_stream, result, requested_model)``.

    Shared by the bearer-key and x-cashu paths. Raises :class:`UpstreamError`
    on bad input or upstream failure.
    """
    if not request_body:
        raise UpstreamError(
            "Missing request body for /v1/messages", status_code=400
        )

    try:
        body: dict = json.loads(request_body)
    except json.JSONDecodeError as exc:
        raise UpstreamError(
            f"Invalid JSON in /v1/messages body: {exc}", status_code=400
        ) from exc

    body.pop("model", None)
    # `stream` here is what the **client** asked for. Upstream is always
    # streamed (see `upstream_stream` below); when the client asked for a
    # non-streaming response we drain and aggregate the events into a
    # single Anthropic Message dict before returning. This sidesteps
    # provider-specific non-streaming caps (e.g. Fireworks rejects
    # `max_tokens > 4096` unless `stream=true`).
    client_stream = bool(body.pop("stream", False))
    upstream_stream = True

    dropped: dict[str, Any] = {}
    for field in ANTHROPIC_ONLY_FIELDS:
        if field in body:
            dropped[field] = body.pop(field)
    if dropped:
        logger.debug(
            "Dropped anthropic-only fields before litellm dispatch",
            extra={"dropped_keys": sorted(dropped.keys())},
        )

    # Convention: `model.id` is the canonical upstream model name;
    # `forwarded_model_id` is the public alias the internal API exposes
    # and echoes back to the client.
    requested_model = (
        (model_obj.forwarded_model_id or model_obj.id) if model_obj else None
    )
    upstream_model = transform_model_name(model_obj.id)
    litellm_model = f"{provider_prefix}{upstream_model}"

    kwargs: dict = {
        "model": litellm_model,
        "api_base": base_url,
        "api_key": api_key,
        "stream": upstream_stream,
        **body,
    }

    logger.info(
        "Dispatching /v1/messages via litellm",
        extra={
            "model": litellm_model,
            "resolved_provider": provider_prefix.rstrip("/"),
            "client_stream": client_stream,
            "upstream_stream": upstream_stream,
            **(log_extra or {}),
        },
    )

    try:
        result = await litellm.anthropic.messages.acreate(**kwargs)
    except Exception as exc:
        exc_message = getattr(exc, "message", None) or str(exc) or repr(exc)
        exc_status = getattr(exc, "status_code", None)
        exc_response = getattr(exc, "response", None)
        response_text = None
        if exc_response is not None:
            try:
                response_text = getattr(exc_response, "text", str(exc_response))
            except Exception:
                response_text = "<unreadable>"
        logger.error(
            "litellm dispatch failed",
            extra={
                "error": exc_message,
                "error_type": type(exc).__name__,
                "status_code": exc_status,
                "llm_provider": getattr(exc, "llm_provider", None),
                "body": getattr(exc, "body", None),
                "response_text": response_text,
                "model": litellm_model,
                "api_base": base_url,
            },
        )
        raise UpstreamError(
            f"Upstream error via litellm: {exc_message}",
            status_code=exc_status if isinstance(exc_status, int) else 502,
        ) from exc

    if not client_stream and hasattr(result, "__aiter__"):
        # Client asked for a non-streaming response but we always stream
        # from upstream — drain the events into a single Anthropic Message
        # dict so the rest of the pipeline can treat it as if upstream had
        # returned non-streaming. Some litellm adapters return a
        # non-streaming dict even when ``stream=True``; in that case,
        # leave the result as-is.
        try:
            aggregated: Any = await aggregate_anthropic_events_to_message(
                cast(AsyncIterator[Any], result)
            )
        except Exception as exc:
            logger.error(
                "Failed to aggregate streamed events into message",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "model": litellm_model,
                },
            )
            raise UpstreamError(
                f"Failed to aggregate upstream stream: {exc}",
                status_code=502,
            ) from exc
        return client_stream, aggregated, requested_model

    return client_stream, result, requested_model
