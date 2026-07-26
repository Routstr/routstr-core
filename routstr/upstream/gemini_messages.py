"""Custom /v1/messages dispatcher for Gemini's OpenAI-compat endpoint.

Why this exists
---------------

Gemini 2.5 / 3 thinking models reject inbound ``functionCall`` parts that
lack a ``thought_signature`` field once any prior turn in the conversation
contains a function call. Anthropic-Messages clients (Claude Code etc.)
have no concept of thought signatures, so multi-turn tool conversations
fail with::

    Function call is missing a thought_signature in functionCall parts.

Google's published escape hatch (https://ai.google.dev/gemini-api/docs/
thought-signatures, FAQ #1) is the dummy signature
``"skip_thought_signature_validator"`` placed at
``tool_calls[i].extra_content.google.thought_signature`` for every tool
call in the request. The hatch is documented specifically for
"transferring a trace from a different model that does not include thought
signatures" — exactly our case.

Why we can't reach the wire via litellm
---------------------------------------

``litellm.anthropic.messages.acreate`` flows through the openai SDK, whose
pydantic ``ChatCompletionMessageToolCall`` model silently drops unknown
fields like ``extra_content``. Litellm has no openai-compat translator
that emits ``extra_content.google.thought_signature``. So we bypass both
litellm and the openai SDK at the transport layer.

Pipeline
--------

1. Translate Anthropic body → OpenAI body via litellm's
   ``AnthropicAdapter`` (the same translator
   ``litellm.anthropic.messages.acreate`` uses internally).
2. Inject ``extra_content.google.thought_signature`` on every
   ``tool_calls[]`` entry.
3. Set ``reasoning_effort="none"`` to disable Gemini's thinking pass.
4. POST directly to ``{base_url}/chat/completions`` with ``stream=true``
   via ``httpx`` (preserves arbitrary fields verbatim).
5. Translate OpenAI streaming chunks → Anthropic SSE events.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, Callable

import httpx

from ..core import get_logger
from ..core.exceptions import UpstreamError
from ..payment.models import Model
from .messages_dispatch import (
    ANTHROPIC_ONLY_FIELDS,
    aggregate_anthropic_events_to_message,
)

logger = get_logger(__name__)

DUMMY_THOUGHT_SIGNATURE = "skip_thought_signature_validator"

# Mapping: OpenAI finish_reason → Anthropic stop_reason
_FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def inject_thought_signatures(messages: list[dict]) -> None:
    """Add ``extra_content.google.thought_signature`` to every tool_call.

    Mutates ``messages`` in place. Idempotent: existing signatures are not
    overwritten.
    """
    for msg in messages:
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            extra = tc.get("extra_content")
            if not isinstance(extra, dict):
                extra = tc["extra_content"] = {}
            google_cfg = extra.get("google")
            if not isinstance(google_cfg, dict):
                google_cfg = extra["google"] = {}
            google_cfg.setdefault("thought_signature", DUMMY_THOUGHT_SIGNATURE)


def _translate_anthropic_to_openai(body: dict, model: str) -> dict:
    """Use litellm's translator to convert an Anthropic /messages body to
    OpenAI /chat/completions kwargs.

    Imported lazily because the litellm internal path is heavy and not
    needed for any other code path in routstr.
    """
    from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (  # noqa: E501
        AnthropicAdapter,
    )

    kwargs = {"model": model, **body}
    translated = AnthropicAdapter().translate_completion_input_params(kwargs)
    if translated is None:
        raise UpstreamError(
            "Failed to translate Anthropic body to OpenAI format",
            status_code=500,
        )
    return dict(translated)


def _sse_event(event_type: str, payload: dict) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n".encode()


async def _openai_chunks_to_anthropic_events(
    line_iter: AsyncIterator[str], requested_model: str | None
) -> AsyncGenerator[bytes, None]:
    """Translate an OpenAI chat-completions SSE byte stream into the
    Anthropic-Messages SSE event sequence.

    Maintains per-chunk state across:

    * one optional text content block (lazy-opened on first text delta)
    * any number of tool_use blocks indexed by openai's ``delta.tool_calls[].index``
    * final ``stop_reason`` / ``usage`` carried out via ``message_delta`` /
      ``message_stop``
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    started = False
    text_block_idx: int | None = None
    tool_block_indices: dict[int, int] = {}
    next_block_idx = 0
    final_finish_reason: str | None = None
    final_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    def open_text_block() -> bytes:
        nonlocal text_block_idx, next_block_idx
        text_block_idx = next_block_idx
        next_block_idx += 1
        return _sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": text_block_idx,
                "content_block": {"type": "text", "text": ""},
            },
        )

    def open_tool_block(delta_idx: int, tc: dict) -> bytes:
        nonlocal next_block_idx
        block_idx = next_block_idx
        next_block_idx += 1
        tool_block_indices[delta_idx] = block_idx
        fn = tc.get("function") or {}
        return _sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": block_idx,
                "content_block": {
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": fn.get("name") or "",
                    "input": {},
                },
            },
        )

    def close_block(idx: int) -> bytes:
        return _sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": idx},
        )

    async for raw_line in line_iter:
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].lstrip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue

        if not started:
            started = True
            yield _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": chunk.get("id") or msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": requested_model or chunk.get("model") or "",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                        },
                    },
                },
            )

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            out_tok = usage.get("completion_tokens") or usage.get("output_tokens") or 0
            if in_tok:
                final_usage["input_tokens"] = int(in_tok)
            if out_tok:
                final_usage["output_tokens"] = int(out_tok)

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            delta = {}

        # Text delta
        text = delta.get("content")
        if isinstance(text, str) and text:
            if text_block_idx is None:
                yield open_text_block()
            yield _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": text_block_idx,
                    "delta": {"type": "text_delta", "text": text},
                },
            )

        # Tool call deltas
        tool_calls_delta = delta.get("tool_calls")
        if isinstance(tool_calls_delta, list):
            for tc in tool_calls_delta:
                if not isinstance(tc, dict):
                    continue
                d_idx = int(tc.get("index") or 0)
                if d_idx not in tool_block_indices:
                    yield open_tool_block(d_idx, tc)
                block_idx = tool_block_indices[d_idx]
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": block_idx,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": args,
                            },
                        },
                    )

        finish = choice.get("finish_reason")
        if finish:
            final_finish_reason = finish

    # Close any open content blocks
    if text_block_idx is not None:
        yield close_block(text_block_idx)
    for block_idx in tool_block_indices.values():
        yield close_block(block_idx)

    # message_delta with stop_reason and usage
    stop_reason = _FINISH_TO_STOP.get(final_finish_reason or "", "end_turn")
    yield _sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": final_usage,
        },
    )
    yield _sse_event("message_stop", {"type": "message_stop"})


async def _post_and_stream(
    base_url: str,
    api_key: str,
    payload: dict,
    log_extra: dict[str, Any] | None,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """POST to upstream chat-completions and return (client, response) for
    streaming. Caller is responsible for closing both."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=120.0))
    try:
        request = client.build_request(
            "POST",
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        response = await client.send(request, stream=True)
    except Exception as exc:
        await client.aclose()
        logger.error(
            "Gemini messages dispatch HTTP error",
            extra={"error": str(exc), "url": url, **(log_extra or {})},
        )
        raise UpstreamError(
            f"Failed to reach Gemini upstream: {exc}", status_code=502
        ) from exc

    if response.status_code >= 400:
        try:
            body_bytes = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        body_text = body_bytes.decode("utf-8", errors="replace")
        logger.error(
            "Gemini messages dispatch upstream error",
            extra={
                "status_code": response.status_code,
                "body": body_text[:1000],
                "url": url,
                **(log_extra or {}),
            },
        )
        raise UpstreamError(
            f"Upstream error via gemini compat: {body_text}",
            status_code=response.status_code,
        )

    return client, response


async def dispatch_gemini_messages(
    *,
    request_body: bytes | None,
    model_obj: Model,
    base_url: str,
    api_key: str,
    transform_model_name: Callable[[str], str],
    log_extra: dict[str, Any] | None = None,
) -> tuple[bool, Any, str | None]:
    """Dispatch a /v1/messages request to Gemini's OpenAI-compat endpoint
    with thought-signature injection.

    Returns ``(client_stream, result, requested_model)`` where ``result``
    is either an ``AsyncIterator[bytes]`` of Anthropic-format SSE events
    (for streaming clients) or an Anthropic Message dict (after the caller
    aggregates).
    """
    if not request_body:
        raise UpstreamError("Missing request body for /v1/messages", status_code=400)

    try:
        body: dict = json.loads(request_body)
    except json.JSONDecodeError as exc:
        raise UpstreamError(
            f"Invalid JSON in /v1/messages body: {exc}", status_code=400
        ) from exc

    body.pop("model", None)
    client_stream = bool(body.pop("stream", False))

    # Anthropic-Messages-only fields that don't translate to OpenAI
    # Chat Completions. litellm's translator passes through unknown
    # top-level fields verbatim and Gemini's compat surface 400s on
    # unknown names like ``context_management`` / ``output_config``.
    dropped: dict[str, Any] = {}
    for field in ANTHROPIC_ONLY_FIELDS:
        if field in body:
            dropped[field] = body.pop(field)
    if dropped:
        logger.debug(
            "Dropped anthropic-only fields before gemini compat dispatch",
            extra={"dropped_keys": sorted(dropped.keys())},
        )

    requested_model = (
        (model_obj.forwarded_model_id or model_obj.id) if model_obj else None
    )
    upstream_model = transform_model_name(model_obj.id)

    openai_kwargs = _translate_anthropic_to_openai(body, upstream_model)

    messages = openai_kwargs.get("messages") or []
    if isinstance(messages, list):
        inject_thought_signatures(messages)

    # Disable Gemini's thinking pass; the dummy signature already lifts
    # validation, but skipping thinking entirely avoids degraded model
    # output and keeps tool-calling deterministic.
    openai_kwargs.setdefault("reasoning_effort", "none")
    openai_kwargs["stream"] = True
    openai_kwargs["model"] = upstream_model
    # OpenAI-compat backends (including Gemini's) only emit a final
    # ``usage`` chunk when the request opts in via this flag. Without it
    # the cost-calculation pipeline can't read real token counts and
    # falls back to MaxCostData billing.
    existing_stream_options = openai_kwargs.get("stream_options")
    merged_stream_options = (
        dict(existing_stream_options)
        if isinstance(existing_stream_options, dict)
        else {}
    )
    merged_stream_options.setdefault("include_usage", True)
    openai_kwargs["stream_options"] = merged_stream_options

    logger.info(
        "Dispatching /v1/messages via gemini compat (httpx)",
        extra={
            "model": upstream_model,
            "client_stream": client_stream,
            "messages_with_tool_calls": sum(
                1 for m in messages if isinstance(m, dict) and m.get("tool_calls")
            ),
            **(log_extra or {}),
        },
    )

    http_client, response = await _post_and_stream(
        base_url, api_key, openai_kwargs, log_extra
    )

    async def line_iter() -> AsyncGenerator[str, None]:
        try:
            async for line in response.aiter_lines():
                yield line
        finally:
            await response.aclose()
            await http_client.aclose()

    anthropic_event_iter = _openai_chunks_to_anthropic_events(
        line_iter(), requested_model
    )

    if not client_stream:
        # Aggregate the Anthropic SSE byte stream into a single Message dict
        # so the rest of the pipeline (cost calc, metadata injection,
        # response building) can treat it identically to a non-streaming
        # litellm response.
        try:
            aggregated = await aggregate_anthropic_events_to_message(
                anthropic_event_iter
            )
        except Exception as exc:
            logger.error(
                "Failed to aggregate Gemini compat events into message",
                extra={"error": str(exc), **(log_extra or {})},
            )
            raise UpstreamError(
                f"Failed to aggregate upstream stream: {exc}",
                status_code=502,
            ) from exc
        return client_stream, aggregated, requested_model

    return client_stream, anthropic_event_iter, requested_model
