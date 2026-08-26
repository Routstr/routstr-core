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
from ..payment.helpers import estimate_prompt_tokens, estimate_tokens
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


def _model_name(model_obj: Model | None, body: dict[str, Any]) -> str:
    if model_obj is not None:
        return model_obj.forwarded_model_id or model_obj.id or ""
    body_model = body.get("model")
    return body_model if isinstance(body_model, str) else ""


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


def _count_text_with_litellm(model: str, text: str) -> int:
    return int(
        litellm.token_counter(
            model=model,
            text=text,
            count_response_tokens=True,
        )
    )


def _generated_text(value: object) -> list[str]:
    """Extract generated text/tool arguments without counting response metadata."""
    generated_keys = {
        "arguments",
        "content",
        "delta",
        "output_text",
        "partial_json",
        "reasoning",
        "reasoning_content",
        "text",
        "thinking",
    }
    parts: list[str] = []

    def walk(item: object, key: str | None = None) -> None:
        if isinstance(item, str):
            if key in generated_keys:
                parts.append(item)
            return
        if isinstance(item, list):
            for child in item:
                walk(child, key)
            return
        if isinstance(item, dict):
            for child_key, child in item.items():
                walk(child, child_key)

    walk(value)
    return parts


class MissingUsageEstimator:
    """Estimate billable usage when an upstream omits its usage trailer.

    The reservation is deliberately absent from this class: it is an
    authorization ceiling, not an input to usage measurement.
    """

    def __init__(self, request_body: bytes | None, model_obj: Model | None) -> None:
        self.body = _parse_request_body(request_body)
        self.model_name = _model_name(model_obj, self.body)
        self._output_parts: list[str] = []
        self._input_tokens: int | None = None

    def _estimate_input_tokens(self) -> int:
        if self._input_tokens is not None:
            return self._input_tokens
        try:
            self._input_tokens = _count_with_litellm(self.model_name, self.body)
        except Exception as exc:
            self._input_tokens = estimate_prompt_tokens(self.body)
            logger.debug(
                "litellm request token count failed; using local estimator",
                extra={
                    "model": self.model_name,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "estimated_tokens": self._input_tokens,
                },
            )
        return self._input_tokens

    @property
    def output_text(self) -> str:
        return "".join(self._output_parts)

    def observe(self, response_data: object) -> None:
        if isinstance(response_data, dict):
            event_type = response_data.get("type")
            if isinstance(event_type, str) and event_type.endswith(".done"):
                # Responses API ``*.done`` events repeat text already streamed
                # via ``*.delta`` events; counting both would double-bill.
                return
        self._output_parts.extend(_generated_text(response_data))

    def billing_data(
        self,
        response_data: dict[str, Any] | None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Use measured usage when present, otherwise return a local estimate."""
        if isinstance(response_data, dict):
            usage = response_data.get("usage")
            if not isinstance(usage, dict):
                nested = response_data.get("response")
                usage = nested.get("usage") if isinstance(nested, dict) else None
            if isinstance(usage, dict) and usage:
                return {
                    "model": model or response_data.get("model") or self.model_name,
                    "usage": usage,
                }
            if not self._output_parts:
                self.observe(response_data)
        return self.response_data(model)

    def response_data(self, model: str | None = None) -> dict[str, Any]:
        text = self.output_text
        try:
            output_tokens = (
                _count_text_with_litellm(self.model_name, text) if text else 0
            )
        except Exception as exc:
            output_tokens = len(text) // 3
            logger.debug(
                "litellm response token count failed; using local estimator",
                extra={
                    "model": self.model_name,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "estimated_tokens": output_tokens,
                },
            )

        input_tokens = max(0, int(self._estimate_input_tokens()))
        output_tokens = max(0, int(output_tokens))
        return {
            "model": model or self.model_name or "unknown",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "estimated": True,
            },
        }

    def openai_response_data(self, model: str | None = None) -> dict[str, Any]:
        """Same estimate in the OpenAI chat-completions usage dialect."""
        data = self.response_data(model)
        usage = data["usage"]
        return {
            "model": data["model"],
            "usage": {
                "prompt_tokens": usage["input_tokens"],
                "completion_tokens": usage["output_tokens"],
                "total_tokens": usage["total_tokens"],
                "estimated": True,
            },
        }


def count_tokens_locally(
    request_body: bytes | None,
    model_obj: Model | None,
) -> Response:
    """Return an Anthropic-compatible count_tokens response without
    touching the upstream. Always returns 200; never raises."""
    body = _parse_request_body(request_body)

    model_name = _model_name(model_obj, body)

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
