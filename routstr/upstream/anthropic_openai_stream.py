"""OpenAI chat-completions stream -> Anthropic Messages events.

Port of the two litellm 1.101.2 layers the ``/v1/messages`` bridge stacks on
an OpenAI-compatible stream:

* ``ChatStreamNormalizer`` — ``CustomStreamWrapper``: drops empty chunks,
  holds ``finish_reason`` until the delta is empty or the stream ends, and
  reports usage once at the end (estimated with tiktoken when the upstream
  sent none).
* ``AnthropicEventTranslator`` — ``AnthropicStreamWrapper``: the content-block
  state machine that turns those chunks into Anthropic SSE events.

Chunks are plain dicts: ``{"choices": [{"delta": {...}, "finish_reason": str
| None}], "usage": dict | None}``.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

from ..payment.usage import parse_token_count
from .anthropic_openai import (
    THOUGHT_SIGNATURE_SEPARATOR,
    is_empty_unsigned_thinking_block,
    normalize_tool_use_id,
)

_FINISH_REASON_MAP = {
    "stop_sequence": "stop",
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "compaction": "length",
    "COMPLETE": "stop",
    "ERROR_TOXIC": "content_filter",
    "ERROR": "stop",
    "eos_token": "stop",
    "eos": "stop",
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "FINISH_REASON_UNSPECIFIED": "stop",
    "MALFORMED_FUNCTION_CALL": "stop",
    "LANGUAGE": "content_filter",
    "OTHER": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "IMAGE_PROHIBITED_CONTENT": "content_filter",
    "TOO_MANY_TOOL_CALLS": "stop",
    "MALFORMED_RESPONSE": "stop",
    "network_error": "stop",
    "sensitive": "content_filter",
    "guardrail_intervened": "content_filter",
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "function_call",
    "content_filter": "content_filter",
    "content_filtered": "content_filter",
    "error": "stop",
}


def map_finish_reason(finish_reason: str) -> str:
    return _FINISH_REASON_MAP.get(finish_reason, "stop")


def anthropic_stop_reason(finish_reason: str, *, refusal: bool = False) -> str:
    """``refusal`` maps ``content_filter`` as the old Gemini translator did."""
    if refusal and finish_reason == "content_filter":
        return "refusal"
    return {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}.get(
        finish_reason, "end_turn"
    )


class StreamPayloadError(Exception):
    """An error object the upstream sent inside an HTTP 200 stream."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def chunk_error(chunk: dict, *, use_code: bool = True) -> StreamPayloadError | None:
    """An ``{"error": ...}`` stream payload as an exception.

    litellm's openai-SDK providers surfaced every such payload as a 500;
    its HTTP-handler providers took the status from ``error.code``.
    """
    error = chunk.get("error")
    if not error:
        return None
    if not isinstance(error, dict):
        return StreamPayloadError(str(error), 500)
    message = error.get("message")
    code = error.get("code")
    status = 500
    if use_code and isinstance(code, int) and 400 <= code < 600:
        status = code
    return StreamPayloadError(
        message if isinstance(message, str) else json.dumps(error), status
    )


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    return 0


def _usage_fields(raw: dict) -> dict[str, Any]:
    """Collapse one provider usage dict the way ``litellm.Usage`` does."""
    details = raw.get("prompt_tokens_details")
    details = dict(details) if isinstance(details, dict) else None
    for key in ("prompt_cache_hit_tokens", "cache_read_input_tokens"):
        if isinstance(raw.get(key), int):
            details = {**(details or {}), "cached_tokens": raw[key]}
    if isinstance(raw.get("cache_creation_input_tokens"), int):
        details = {
            **(details or {}),
            "cache_write_tokens": raw["cache_creation_input_tokens"],
        }
    explicit_read = raw.get("cache_read_input_tokens")
    if not isinstance(explicit_read, int):
        explicit_read = raw.get("prompt_cache_hit_tokens")
    return {
        "prompt_tokens": parse_token_count(raw.get("prompt_tokens")),
        "completion_tokens": parse_token_count(raw.get("completion_tokens")),
        "cache_read_input_tokens": explicit_read
        if isinstance(explicit_read, int)
        else None,
        "cache_creation_input_tokens": raw.get("cache_creation_input_tokens")
        if isinstance(raw.get("cache_creation_input_tokens"), int)
        else None,
        "prompt_tokens_details": details,
        "server_tool_use": raw.get("server_tool_use"),
    }


def anthropic_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Merged usage (``_usage_fields`` shape) -> Anthropic ``usage`` dict."""
    details = usage.get("prompt_tokens_details") or {}
    cache_read = _positive_int(usage.get("cache_read_input_tokens")) or _positive_int(
        details.get("cached_tokens")
    )
    cache_write = _positive_int(usage.get("cache_creation_input_tokens")) or next(
        (
            n
            for n in (
                _positive_int(details.get("cache_creation_tokens")),
                _positive_int(details.get("cache_write_tokens")),
            )
            if n
        ),
        0,
    )
    server_tool_use = usage.get("server_tool_use")
    web_search = _positive_int(
        server_tool_use.get("web_search_requests")
        if isinstance(server_tool_use, dict)
        else None
    ) or _positive_int(details.get("web_search_requests"))
    result: dict[str, Any] = {
        "input_tokens": max(
            (usage.get("prompt_tokens") or 0) - cache_read - cache_write, 0
        ),
        "output_tokens": usage.get("completion_tokens") or 0,
    }
    if cache_write:
        result["cache_creation_input_tokens"] = cache_write
    if cache_read:
        result["cache_read_input_tokens"] = cache_read
    if web_search:
        result["server_tool_use"] = {"web_search_requests": web_search}
    return result


# --------------------------------------------------------------------------
# CustomStreamWrapper
# --------------------------------------------------------------------------


def normalize_delta(raw: object) -> dict[str, Any]:
    delta = dict(raw) if isinstance(raw, dict) else {}
    if delta.get("reasoning_content") is None and "reasoning" in delta:
        delta["reasoning_content"] = delta.pop("reasoning")
    if delta.get("reasoning_content") is None:
        delta.pop("reasoning_content", None)
    if delta.get("thinking_blocks") is None:
        delta.pop("thinking_blocks", None)
    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, (list, tuple)):
        normalized: list[dict] = []
        next_index = 0
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            call = dict(call)
            if call.get("index") is None:
                call["index"] = next_index
                next_index += 1
            if call.get("type") is None:
                call["type"] = "function"
            function = call.get("function")
            if isinstance(function, dict):
                arguments = function.get("arguments") or ""
                call["function"] = {
                    "name": function.get("name"),
                    "arguments": arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments),
                    **{
                        k: v
                        for k, v in function.items()
                        if k not in ("name", "arguments")
                    },
                }
            normalized.append(call)
        delta["tool_calls"] = normalized
    else:
        delta["tool_calls"] = None
    delta.setdefault("content", None)
    return delta


def _delta_is_empty(delta: dict) -> bool:
    return not (
        delta.get("content")
        or delta.get("tool_calls") is not None
        or delta.get("function_call") is not None
    )


class ChatStreamNormalizer:
    """Port of ``CustomStreamWrapper`` for OpenAI-compatible chunks.

    ``http_handler`` mirrors litellm's provider handlers that build
    ``StreamingChoices`` (which maps the reason) before the wrapper sees it.
    ``estimate_usage(completion_text)`` returns ``(prompt, completion)`` token
    estimates for upstreams that report no usage.
    """

    def __init__(
        self,
        *,
        http_handler: bool = False,
        estimate_usage: Callable[[str], tuple[int, int]] | None = None,
    ) -> None:
        self._http_handler = http_handler
        self._estimate_usage = estimate_usage
        self._received_finish: str | None = None
        self._sent_first = False
        self._sent_last = False
        self._usages: list[dict[str, Any]] = []
        self._text: list[str] = []
        self._tool_calls: list[dict[str, str]] = []
        self._open_calls: dict[int, dict[str, str]] = {}
        # litellm builds the final usage from the chunks it handed on; with
        # none there is nothing to estimate from and usage stays zero.
        self._emitted = False

    def feed(self, chunk: dict) -> list[dict]:
        if (error := chunk_error(chunk, use_code=self._http_handler)) is not None:
            raise error
        choices = chunk.get("choices") or []
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usages.append(_usage_fields(usage))
        if not choices or not isinstance(choices[0], dict):
            return []
        choice = choices[0]
        finish = choice.get("finish_reason")
        if finish and self._http_handler:
            finish = map_finish_reason(finish)
        if finish == "error":
            raise StreamPayloadError(
                "raised a streaming error - finish_reason: error, no content "
                f"string given. Received Chunk={chunk}",
                500,
            )
        if finish:
            self._received_finish = finish
        delta = normalize_delta(choice.get("delta"))
        content = delta.get("content")
        non_empty = (
            (isinstance(content, str) and len(content) > 0)
            or bool(delta.get("tool_calls"))
            or delta.get("function_call") is not None
            or "reasoning_content" in delta
            or delta.get("provider_specific_fields") is not None
            or delta.get("annotations") is not None
            or delta.get("reasoning_items") is not None
            or (not self._sent_first and delta.get("role") is not None)
        )
        if non_empty:
            self._strip_role(delta)
            self._record(delta)
            return [{"choices": [{"delta": delta, "finish_reason": None}]}]
        if self._received_finish is not None:
            if self._sent_last:
                return []
            if not _delta_is_empty(delta):
                self._record(delta)
                return [{"choices": [{"delta": delta, "finish_reason": None}]}]
            self._sent_last = True
            self._emitted = True
            return [
                {
                    "choices": [
                        {
                            "delta": {"content": None, "tool_calls": None},
                            "finish_reason": map_finish_reason(self._received_finish),
                        }
                    ]
                }
            ]
        if delta.get("tool_calls") is not None:
            self._strip_role(delta)
            self._record(delta)
            return [{"choices": [{"delta": delta, "finish_reason": None}]}]
        return []

    def finish(self) -> list[dict]:
        chunks: list[dict] = []
        if not self._sent_last:
            self._sent_last = True
            chunks.append(
                {
                    "choices": [
                        {
                            "delta": {"content": None, "tool_calls": None},
                            "finish_reason": self._received_finish or "stop",
                        }
                    ]
                }
            )
        chunks.append(
            {
                "choices": [
                    {
                        "delta": {"content": None, "tool_calls": None},
                        "finish_reason": None,
                    }
                ],
                "usage": self.usage(),
            }
        )
        return chunks

    def usage(self) -> dict[str, Any]:
        merged: dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "prompt_tokens_details": None,
            "server_tool_use": None,
        }
        for usage in self._usages:
            if usage["prompt_tokens"] > 0:
                merged["prompt_tokens"] = usage["prompt_tokens"]
            if usage["completion_tokens"] > 0:
                merged["completion_tokens"] = usage["completion_tokens"]
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                value = usage[key]
                if value is not None and (value > 0 or merged[key] is None):
                    merged[key] = value
            if usage["server_tool_use"] is not None:
                merged["server_tool_use"] = usage["server_tool_use"]
            merged["prompt_tokens_details"] = (
                usage["prompt_tokens_details"] or merged["prompt_tokens_details"]
            )
        if (
            (self._emitted or self._usages)
            and (not merged["prompt_tokens"] or not merged["completion_tokens"])
            and self._estimate_usage is not None
        ):
            try:
                prompt, completion = self._estimate_usage(self._completion_text())
            except Exception:
                prompt, completion = 0, 0
            merged["prompt_tokens"] = merged["prompt_tokens"] or prompt
            merged["completion_tokens"] = merged["completion_tokens"] or completion
        return merged

    def _strip_role(self, delta: dict) -> None:
        if not self._sent_first:
            delta["role"] = "assistant"
            self._sent_first = True
        else:
            delta.pop("role", None)

    def _record(self, delta: dict) -> None:
        self._emitted = True
        if isinstance(delta.get("content"), str):
            self._text.append(delta["content"])
        for call in delta.get("tool_calls") or ():
            self._tool_text(call)

    def _tool_text(self, call: dict) -> None:
        """Merge tool-call fragments by index, a new ``id`` opening a new call."""
        function = call.get("function") or {}
        index = call.get("index", 0)
        merged = self._open_calls.get(index)
        if merged is None or (call.get("id") and call["id"] != merged["id"]):
            merged = {"id": call.get("id") or "", "name": "", "arguments": ""}
            self._open_calls[index] = merged
            self._tool_calls.append(merged)
        if function.get("name") and not merged["name"]:
            merged["name"] = function["name"]
        if isinstance(function.get("arguments"), str):
            merged["arguments"] += function["arguments"]

    def _completion_text(self) -> str:
        """Text plus each merged tool call as litellm's ``model_dump_json``."""
        calls = (
            json.dumps(
                {
                    "function": {"arguments": c["arguments"], "name": c["name"]},
                    "id": c["id"],
                    "type": "function",
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
            for c in self._tool_calls
        )
        return "".join(self._text) + "".join(calls)


# --------------------------------------------------------------------------
# AnthropicStreamWrapper
# --------------------------------------------------------------------------


def _has_payload(delta: dict, field: str) -> bool:
    return bool(delta.get(field))


def _split_combined(chunk: dict) -> list[dict]:
    """Peel ``finish_reason`` off a chunk that also carries content."""
    choice = chunk["choices"][0]
    delta = choice.get("delta") or {}
    if choice.get("finish_reason") is None or not any(
        _has_payload(delta, f)
        for f in ("content", "tool_calls", "reasoning_content", "thinking_blocks")
    ):
        return [chunk]
    content_chunk = copy.deepcopy(chunk)
    content_chunk["choices"][0]["finish_reason"] = None
    content_chunk.pop("usage", None)
    finish_chunk = copy.deepcopy(chunk)
    finish_delta = finish_chunk["choices"][0]["delta"]
    finish_delta["content"] = None
    for field in ("tool_calls", "reasoning_content", "thinking_blocks"):
        if field in finish_delta:
            finish_delta[field] = None
    return [content_chunk, finish_chunk]


_PAYLOAD_GROUPS = (
    ("reasoning_content", "thinking_blocks"),
    ("content",),
    ("tool_calls",),
)


def _split_by_payload_kind(chunk: dict) -> list[dict]:
    """One chunk per payload kind, in thinking -> text -> tool_use order."""
    choices = chunk.get("choices") or []
    if len(choices) != 1:
        return [chunk]
    delta = choices[0].get("delta")
    if delta is None:
        return [chunk]
    tool_calls = delta.get("tool_calls") or ()
    if tool_calls and not any(
        (c.get("function") or {}).get("name") for c in tool_calls
    ):
        return [chunk]
    groups = [g for g in _PAYLOAD_GROUPS if any(delta.get(f) for f in g)]
    if len(groups) <= 1:
        return [chunk]
    pieces: list[dict] = []
    for index, group in enumerate(groups):
        piece = copy.deepcopy(chunk)
        fields = {f: copy.deepcopy(delta[f]) for f in group if delta.get(f)}
        blocks = fields.get("thinking_blocks")
        if blocks and not any(
            isinstance(b, dict) and b.get("signature") for b in blocks
        ):
            text = "".join(
                b.get("thinking") or ""
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "thinking"
            )
            if text:
                fields = {"reasoning_content": text}
        new_delta: dict[str, Any] = {
            "content": None,
            "tool_calls": None,
            **fields,
        }
        if index == 0 and delta.get("role") is not None:
            new_delta["role"] = delta["role"]
        piece["choices"][0]["delta"] = new_delta
        pieces.append(piece)
    return pieces


def _split_tool_calls(chunk: dict) -> list[dict]:
    """One chunk per tool call, so each call gets its own ``tool_use`` block.

    litellm read only the first call's name and joined every call's
    arguments into it; Ollama and Gemini send parallel calls in one delta.
    """
    choices = chunk.get("choices") or []
    if len(choices) != 1:
        return [chunk]
    delta = choices[0].get("delta") or {}
    tool_calls = delta.get("tool_calls") or ()
    if len(tool_calls) < 2:
        return [chunk]
    pieces: list[dict] = []
    for index, call in enumerate(tool_calls):
        piece = copy.deepcopy(chunk)
        piece_delta = piece["choices"][0]["delta"]
        piece_delta["tool_calls"] = [copy.deepcopy(call)]
        if index:
            piece_delta.pop("role", None)
        pieces.append(piece)
    return pieces


class _ToolCallOrder:
    """Keep each tool call's fragments together in the chunk stream.

    Anthropic content blocks are strictly sequential, but an OpenAI stream may
    interleave argument fragments of parallel calls by ``index``; litellm
    appended a late fragment to whichever block was open. The first call
    streams live; later calls are buffered per call and emitted whole when
    the stream finishes or non-tool content arrives, so every fragment lands
    in its own call. A new ``name`` starts a new call even on a reused index.
    """

    def __init__(self) -> None:
        self._live: dict[str, Any] | None = None
        self._pending: list[dict[str, Any]] = []
        self._calls: list[dict[str, Any]] = []

    def feed(self, chunk: dict) -> list[dict]:
        choices = chunk.get("choices")
        if not choices:
            return [chunk]
        choice = choices[0]
        tool_calls: list[dict] = (choice.get("delta") or {}).get("tool_calls") or []
        # Anything else may open a block, so held calls must come first.
        if choice.get("finish_reason") is not None or len(tool_calls) != 1:
            return [*self.flush(), chunk]
        call = tool_calls[0]
        function = call.get("function") or {}
        index = call.get("index", 0)
        if function.get("name"):
            slot: dict[str, Any] = {
                "index": index,
                "call": copy.deepcopy(call),
                "args": [],
            }
            self._calls.append(slot)
            if self._live is None and not self._pending:
                self._live = slot
                return [chunk]
            slot["args"].append(function.get("arguments") or "")
            self._pending.append(slot)
            return []
        owner = self._latest_call(index)
        if owner is not None and any(owner is p for p in self._pending):
            owner["args"].append(function.get("arguments") or "")
            return []
        # The live call, or a fragment for a call already emitted (litellm's
        # routing: the open block).
        return [chunk]

    def _latest_call(self, index: int) -> dict[str, Any] | None:
        for slot in reversed(self._calls):
            if slot["index"] == index:
                return slot
        return None

    def flush(self) -> list[dict]:
        chunks = []
        for slot in self._pending:
            call = slot["call"]
            call["function"] = {
                **call.get("function", {}),
                "arguments": "".join(slot["args"]),
            }
            delta = {"content": None, "tool_calls": [call]}
            chunks.append({"choices": [{"delta": delta, "finish_reason": None}]})
        self._pending = []
        self._live = None
        return chunks


def _block_start(delta: dict) -> tuple[str, dict]:
    """Content-block type and start body for a chunk's delta."""
    tool_calls = delta.get("tool_calls")
    if tool_calls and tool_calls[0].get("function") is not None:
        call = tool_calls[0]
        raw_id = call.get("id") or str(uuid.uuid4())
        block: dict[str, Any] = {
            "type": "tool_use",
            "id": normalize_tool_use_id(raw_id),
            "name": call["function"].get("name") or "",
            "input": {},
        }
        if THOUGHT_SIGNATURE_SEPARATOR in raw_id:
            signature = raw_id.split(THOUGHT_SIGNATURE_SEPARATOR, 1)[1]
            if signature:
                block["provider_specific_fields"] = {"signature": signature}
        return "tool_use", block
    content = delta.get("content")
    if content is not None and len(content) > 0:
        return "text", {"type": "text", "text": ""}
    blocks = delta.get("thinking_blocks")
    if blocks:
        first = blocks[0]
        if first.get("type") == "thinking":
            return "thinking", {
                "type": "thinking",
                "thinking": first.get("thinking") or "",
                "signature": first.get("signature") or "",
            }
    elif delta.get("reasoning_content"):
        return "thinking", {"type": "thinking", "thinking": "", "signature": ""}
    return "text", {"type": "text", "text": ""}


def _block_delta(delta: dict) -> dict:
    text = ""
    reasoning = ""
    signature = ""
    partial_json: str | None = None
    content = delta.get("content")
    if content is not None and len(content) > 0:
        text += content
    if delta.get("tool_calls"):
        partial_json = ""
        for call in delta["tool_calls"]:
            function = call.get("function")
            if function is not None and function.get("arguments") is not None:
                partial_json += function["arguments"]
    elif "thinking_blocks" in delta:
        for block in delta.get("thinking_blocks") or ():
            if block.get("type") == "thinking":
                reasoning += block.get("thinking") or ""
                signature += block.get("signature") or ""
    elif delta.get("reasoning_content") is not None:
        reasoning += delta["reasoning_content"]
    if partial_json is not None:
        return {"type": "input_json_delta", "partial_json": partial_json}
    if signature:
        return {"type": "signature_delta", "signature": signature}
    if reasoning:
        return {"type": "thinking_delta", "thinking": reasoning}
    return {"type": "text_delta", "text": text}


_DELTA_PAYLOAD = {
    "text_delta": "text",
    "input_json_delta": "partial_json",
    "thinking_delta": "thinking",
    "signature_delta": "signature",
}


def _delta_has_content(event: dict) -> bool:
    if event.get("type") != "content_block_delta":
        return False
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") not in _DELTA_PAYLOAD:
        return False
    return bool(delta.get(_DELTA_PAYLOAD[delta["type"]]))


def _is_blank_delta(chunk: dict) -> bool:
    choice = chunk["choices"][0]
    if choice.get("finish_reason") is not None:
        return False
    delta = choice.get("delta") or {}
    if (
        delta.get("tool_calls")
        or delta.get("content")
        or delta.get("reasoning_content")
    ):
        return False
    blocks = delta.get("thinking_blocks")
    if blocks and any(
        isinstance(b, dict) and not is_empty_unsigned_thinking_block(b) for b in blocks
    ):
        return False
    return True


class AnthropicEventTranslator:
    """Port of litellm's ``AnthropicStreamWrapper`` state machine."""

    def __init__(
        self,
        model: str,
        tool_name_mapping: dict[str, str] | None = None,
        *,
        refusal_on_content_filter: bool = False,
    ) -> None:
        self.model = model
        self._refusal = refusal_on_content_filter
        self.message_id = f"msg_{uuid.uuid4()}"
        self._tool_names = tool_name_mapping or {}
        self._started = False
        self._block_started = False
        self._block_finished = False
        self._block_type = "text"
        self._block_start: dict[str, Any] = {"type": "text", "text": ""}
        self._index = 0
        self._held_stop: dict | None = None
        self._usage_sent = False
        self._stopped = False
        self._tool_order = _ToolCallOrder()

    def start(self) -> list[dict]:
        self._started = True
        return [
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": self.model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }
        ]

    def feed(self, chunk: dict) -> list[dict]:
        events: list[dict] = []
        for combined in _split_combined(chunk) if chunk.get("choices") else [chunk]:
            for piece in (
                _split_by_payload_kind(combined)
                if combined.get("choices")
                else [combined]
            ):
                for call_piece in _split_tool_calls(piece):
                    for ordered in self._tool_order.feed(call_piece):
                        events.extend(self._feed_one(ordered))
        return events

    def _new_block(self, chunk: dict) -> bool:
        choice = chunk["choices"][0]
        if choice.get("finish_reason") is not None:
            return False
        block_type, start = _block_start(choice.get("delta") or {})
        if block_type == "tool_use" and start.get("name"):
            start["name"] = self._tool_names.get(start["name"], start["name"])
        if block_type != self._block_type:
            self._block_type = block_type
            self._block_start = start
            return True
        if block_type == "tool_use" and start.get("name"):
            self._block_start = start
            return True
        return False

    def _translate(self, chunk: dict) -> dict:
        choice = chunk["choices"][0]
        if choice.get("finish_reason") is not None:
            usage = chunk.get("usage")
            return {
                "type": "message_delta",
                "delta": {
                    "stop_reason": anthropic_stop_reason(
                        choice["finish_reason"], refusal=self._refusal
                    )
                },
                "usage": anthropic_usage(usage)
                if usage is not None
                else {"input_tokens": 0, "output_tokens": 0},
            }
        return {
            "type": "content_block_delta",
            "index": self._index,
            "delta": _block_delta(choice.get("delta") or {}),
        }

    def _merge_usage(self, chunk: dict) -> dict:
        assert self._held_stop is not None
        merged = dict(self._held_stop)
        merged.setdefault("delta", {})
        merged["usage"] = anthropic_usage(chunk["usage"])
        return merged

    def _feed_one(self, chunk: dict) -> list[dict]:
        if not chunk.get("choices"):
            if self._held_stop is not None and chunk.get("usage") is not None:
                merged = self._merge_usage(chunk)
                self._usage_sent = True
                self._held_stop = None
                return [merged]
            return []
        events: list[dict] = []
        new_block = self._new_block(chunk)
        opening_first = not self._block_started
        if opening_first and _is_blank_delta(chunk):
            return []
        if opening_first:
            self._block_started = True
            self._block_finished = False
            events.append(
                {
                    "type": "content_block_start",
                    "index": self._index,
                    "content_block": self._block_start,
                }
            )
        elif new_block:
            self._index += 1
        merge_into_held = self._held_stop is not None and chunk.get("usage") is not None
        processed = self._translate(chunk)
        if merge_into_held:
            merged = self._merge_usage(chunk)
            self._usage_sent = True
            self._held_stop = None
            return [*events, merged]
        if self._usage_sent:
            return events
        if new_block and not opening_first and not self._block_finished:
            events.append(
                {"type": "content_block_stop", "index": max(self._index - 1, 0)}
            )
            events.append(
                {
                    "type": "content_block_start",
                    "index": self._index,
                    "content_block": self._block_start,
                }
            )
            if _delta_has_content(processed):
                events.append(processed)
            self._block_finished = False
            return events
        if processed["type"] == "content_block_delta" and not _delta_has_content(
            processed
        ):
            return events
        if processed["type"] == "message_delta" and not self._block_finished:
            events.append({"type": "content_block_stop", "index": self._index})
            self._block_finished = True
            if processed["delta"].get("stop_reason") is not None:
                self._held_stop = processed
            else:
                events.append(processed)
            return events
        events.append(processed)
        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        for ordered in self._tool_order.flush():
            events.extend(self._feed_one(ordered))
        if not self._usage_sent and self._held_stop is not None:
            if not self._block_finished:
                events.append({"type": "content_block_stop", "index": self._index})
                self._block_finished = True
            events.append(self._held_stop)
            self._held_stop = None
        if not self._stopped:
            self._stopped = True
            events.append({"type": "message_stop"})
        return events


def encode_event(event: dict) -> bytes:
    return (
        f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n".encode()
    )


async def anthropic_events_from_chat_stream(
    chunks: AsyncIterator[dict],
    *,
    model: str,
    tool_name_mapping: dict[str, str] | None = None,
    http_handler: bool = False,
    estimate_usage: Callable[[str], tuple[int, int]] | None = None,
    prepare_chunk: Callable[[dict], dict] | None = None,
    refusal_on_content_filter: bool = False,
) -> AsyncIterator[dict]:
    """Translate parsed OpenAI stream chunks into Anthropic event dicts."""
    normalizer = ChatStreamNormalizer(
        http_handler=http_handler, estimate_usage=estimate_usage
    )
    translator = AnthropicEventTranslator(
        model, tool_name_mapping, refusal_on_content_filter=refusal_on_content_filter
    )
    for event in translator.start():
        yield event

    def drain(normalized: Iterable[dict]) -> list[dict]:
        events: list[dict] = []
        for chunk in normalized:
            events.extend(translator.feed(chunk))
        return events

    async for chunk in chunks:
        if prepare_chunk is not None:
            chunk = prepare_chunk(chunk)
        for event in drain(normalizer.feed(chunk)):
            yield event
    for event in drain(normalizer.finish()):
        yield event
    for event in translator.finish():
        yield event
