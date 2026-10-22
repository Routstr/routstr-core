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

Pipeline
--------

1. Translate the Anthropic body to OpenAI chat kwargs with the shared
   translator (``anthropic_openai.anthropic_to_openai``).
2. Inject ``extra_content.google.thought_signature`` on every
   ``tool_calls[]`` entry.
3. Set ``reasoning_effort="none"`` to disable Gemini's thinking pass.
4. POST to ``{base_url}/chat/completions`` with ``stream=true`` and translate
   the OpenAI stream back to Anthropic events, both via the shared
   ``messages_dispatch.send_call``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..core import get_logger
from ..core.exceptions import UpstreamError
from ..payment.models import Model
from .anthropic_openai import anthropic_to_openai
from .messages_dialects import UpstreamCall
from .messages_dispatch import ANTHROPIC_ONLY_FIELDS, send_call

logger = get_logger(__name__)

DUMMY_THOUGHT_SIGNATURE = "skip_thought_signature_validator"
GEMINI_STREAM_READ_TIMEOUT_SECONDS = 120.0


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


def build_gemini_call(
    body: dict, *, base_url: str, api_key: str, upstream_model: str, model: str
) -> UpstreamCall:
    """Translate an Anthropic body into the Gemini chat-completions call."""
    # No prompt_cache_key: the adapter only derives one for providers that
    # declare it, and this call names none.
    try:
        openai_kwargs, name_mapping = anthropic_to_openai(
            {"model": upstream_model, **body}, prompt_cache_key=False
        )
    except ValueError as exc:
        raise UpstreamError(str(exc), status_code=400) from exc
    messages = openai_kwargs.get("messages") or []
    inject_thought_signatures(messages)

    # Disable Gemini's thinking pass; the dummy signature already lifts
    # validation, but skipping thinking entirely avoids degraded model
    # output and keeps tool-calling deterministic.
    openai_kwargs.setdefault("reasoning_effort", "none")
    openai_kwargs["stream"] = True
    openai_kwargs["model"] = upstream_model
    # OpenAI-compat backends (including Gemini's) only emit a final ``usage``
    # chunk when the request opts in; without it billing falls back to
    # estimates.
    stream_options = openai_kwargs.get("stream_options")
    openai_kwargs["stream_options"] = {
        **(stream_options if isinstance(stream_options, dict) else {}),
        "include_usage": True,
    }
    return UpstreamCall(
        wire="openai",
        provider="gemini",
        url=f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        body=openai_kwargs,
        model=model,
        tool_name_mapping=name_mapping,
        refusal_on_content_filter=True,
        skip_malformed_events=True,
    )


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
    (for streaming clients) or an Anthropic Message dict.
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

    # Gemini's compat surface 400s on Anthropic-only fields the translator
    # would otherwise copy through verbatim.
    dropped = sorted(field for field in ANTHROPIC_ONLY_FIELDS if field in body)
    for field in dropped:
        body.pop(field)
    if dropped:
        logger.debug(
            "Dropped anthropic-only fields before gemini compat dispatch",
            extra={"dropped_keys": dropped},
        )

    requested_model = (
        (model_obj.forwarded_model_id or model_obj.id) if model_obj else None
    )
    upstream_model = transform_model_name(model_obj.id)
    call = build_gemini_call(
        body,
        base_url=base_url,
        api_key=api_key,
        upstream_model=upstream_model,
        model=requested_model or upstream_model,
    )
    logger.info(
        "Dispatching /v1/messages via gemini compat",
        extra={
            "model": upstream_model,
            "client_stream": client_stream,
            "messages_with_tool_calls": sum(
                1 for m in call.body["messages"] if m.get("tool_calls")
            ),
            **(log_extra or {}),
        },
    )
    return await send_call(
        call,
        client_stream=client_stream,
        requested_model=requested_model,
        read_timeout=GEMINI_STREAM_READ_TIMEOUT_SECONDS,
        log_extra={"model": upstream_model, **(log_extra or {})},
    )
