"""Local handling of Anthropic ``/v1/messages/count_tokens`` for upstreams
that do not natively expose the endpoint.

Most non-Anthropic upstreams (OpenAI-compat, Gemini OpenAI-compat,
OpenRouter chat-completions, generic providers) return 400/404 when asked
to ``POST /messages/count_tokens``. Claude Code and other Anthropic SDK
clients call this endpoint before each turn to size context windows and
trigger compaction, so a failure breaks the whole chat.

We answer locally. ``litellm.token_counter`` understands the Anthropic
message shape and the per-model tokenizers, so we prefer it. If it raises
(unknown model, encoding lookup failure, ...), we fall back to the
project's own ``estimate_tokens`` heuristic, which is always defined and
never raises.
"""

from __future__ import annotations

import json
from typing import Any

import litellm
from fastapi.responses import Response

from ..core import get_logger
from ..payment.helpers import estimate_tokens
from ..payment.models import Model

logger = get_logger(__name__)


def _parse_request_body(request_body: bytes | None) -> dict[str, Any]:
    if not request_body:
        return {}
    try:
        parsed = json.loads(request_body)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _count_with_litellm(model: str, body: dict[str, Any]) -> int:
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []

    system = body.get("system")
    if isinstance(system, str) and system:
        messages = [{"role": "system", "content": system}, *messages]
    elif isinstance(system, list):
        text = "".join(
            block.get("text", "")
            for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text:
            messages = [{"role": "system", "content": text}, *messages]

    tools = body.get("tools") if isinstance(body.get("tools"), list) else None

    return int(
        litellm.token_counter(
            model=model,
            messages=messages,
            tools=tools,
        )
    )


def count_tokens_locally(
    request_body: bytes | None,
    model_obj: Model | None,
) -> Response:
    """Return an Anthropic-compatible count_tokens response without
    touching the upstream. Always returns 200; never raises."""
    body = _parse_request_body(request_body)

    model_name = ""
    if model_obj is not None:
        model_name = model_obj.forwarded_model_id or model_obj.id or ""
    if not model_name:
        body_model = body.get("model")
        if isinstance(body_model, str):
            model_name = body_model

    input_tokens: int
    try:
        input_tokens = _count_with_litellm(model_name, body)
    except Exception as exc:
        messages = body.get("messages")
        fallback_messages = messages if isinstance(messages, list) else []
        input_tokens = estimate_tokens(fallback_messages)
        logger.debug(
            "litellm token_counter failed; using local estimator",
            extra={
                "model": model_name,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "estimated_tokens": input_tokens,
            },
        )

    payload = {"input_tokens": max(0, int(input_tokens))}
    return Response(
        content=json.dumps(payload).encode(),
        status_code=200,
        media_type="application/json",
    )
