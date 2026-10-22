"""Provider dialects of the ``/v1/messages`` bridge beyond the oracle fixtures.

Expected values were checked against litellm 1.101.2 (the implementation this
replaces) with a differential harness before litellm was removed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from routstr.core.exceptions import UpstreamError
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream import messages_transport
from routstr.upstream.anthropic_openai_stream import (
    AnthropicEventTranslator,
    ChatStreamNormalizer,
)
from routstr.upstream.azure import AzureUpstreamProvider
from routstr.upstream.messages_dialects import build_upstream_call
from routstr.upstream.messages_dispatch import (
    dispatch_anthropic_messages,
    events_from_chunk,
)

TOOL = {
    "name": "t",
    "description": "d",
    "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}}},
}


def _model(model_id: str) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="x",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.0, completion=0.0),
    )


def _body(**extra: Any) -> dict[str, Any]:
    return {"max_tokens": 300, "messages": [{"role": "user", "content": "hi"}], **extra}


def _sse(*events: dict) -> str:
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
        if "type" in e
        else f"data: {json.dumps(e)}\n\n"
        for e in events
    )


async def _run(
    prefix: str,
    base_url: str,
    model_id: str,
    body: dict[str, Any],
    responder: Any,
) -> tuple[Any, list[httpx.Request]]:
    """Dispatch with a mocked upstream; return (events | message | error, requests)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responder(request)

    with respx.mock(assert_all_called=False) as router:
        router.route().mock(side_effect=handler)
        try:
            stream, result, _ = await dispatch_anthropic_messages(
                request_body=json.dumps(body).encode(),
                model_obj=_model(model_id),
                base_url=base_url,
                api_key="k",
                provider_prefix=prefix,
                transform_model_name=lambda name: name,
            )
        except UpstreamError as exc:
            return exc, requests
        if not stream:
            return result, requests
        events: list[dict] = []
        buffer = b""
        async for chunk in result:
            parsed, buffer = events_from_chunk(chunk, buffer)
            events.extend(parsed)
        return events, requests


def _respond(
    text: str, *, status: int = 200, headers: dict[str, str] | None = None
) -> Any:
    return lambda _: httpx.Response(status, headers=headers, text=text)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(messages_transport, "_retry_delay", lambda *_: 0.0)


# --------------------------------------------------------------------------
# OpenAI reasoning families
# --------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["gpt-6", "gpt-6-luna", "gpt-5.5"])
def test_gpt5_and_later_send_max_completion_tokens(model: str) -> None:
    call = build_upstream_call(
        prefix="openai/",
        base_url="http://h/v1",
        api_key="k",
        upstream_model=model,
        body=_body(),
    )
    assert call.body["max_completion_tokens"] == 300
    assert "max_tokens" not in call.body


# --------------------------------------------------------------------------
# Responses API bridge (gpt-5.4+ with function tools)
# --------------------------------------------------------------------------


def _bridged(base_url: str, prefix: str = "openai/", **body: Any) -> Any:
    return build_upstream_call(
        prefix=prefix,
        base_url=base_url,
        api_key="k",
        upstream_model="gpt-5.4",
        body=_body(**body),
    )


def test_gpt54_tools_with_reasoning_go_to_responses_api() -> None:
    call = _bridged(
        "https://api.openai.com/v1",
        system="sys",
        tools=[TOOL],
        tool_choice={"type": "auto"},
        reasoning_effort="high",
        temperature=0.5,
        top_k=4,
        stop_sequences=["X"],
        metadata={"user_id": "u1"},
    )

    assert call.wire == "responses"
    assert call.url == "https://api.openai.com/v1/responses"
    assert call.headers["Authorization"] == "Bearer k"
    assert call.body == {
        "model": "gpt-5.4",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ],
        "instructions": "sys",
        "max_output_tokens": 300,
        "reasoning": {"effort": "high"},
        "stream": True,
        "tool_choice": "auto",
        "tools": [
            {
                "name": "t",
                "parameters": {
                    "type": "object",
                    "properties": {"a": {"type": "integer"}},
                },
                "strict": None,
                "type": "function",
                "description": "d",
            }
        ],
        "user": "u1",
        "prompt_cache_key": "u1",
    }


def test_responses_bridge_on_azure_uses_v1_route_and_api_key() -> None:
    call = _bridged(
        "https://res.openai.azure.com",
        prefix="azure/",
        tools=[TOOL],
        reasoning_effort="low",
    )
    assert call.url == (
        "https://res.openai.azure.com/openai/v1/responses?api-version=preview"
    )
    assert call.headers == {"Content-Type": "application/json", "api-key": "k"}


def test_tool_history_becomes_responses_items() -> None:
    history = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "s"},
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "call_q1", "name": "t", "input": {"a": 0}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_q1", "content": "r"}
            ],
        },
    ]
    call = _bridged("https://api.openai.com/v1", messages=history, tools=[TOOL])

    assert call.body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        },
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "hmm"}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "ok"}],
        },
        {
            "type": "function_call",
            "call_id": "call_q1",
            "name": "t",
            "arguments": '{"a": 0}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_q1",
            "output": [{"type": "input_text", "text": "r"}],
        },
    ]


@pytest.mark.parametrize(
    ("base_url", "extra", "bridged"),
    [
        # A custom OpenAI-compatible host is only bridged on an explicit effort.
        ("http://127.0.0.1:3091/v1", {}, False),
        ("http://127.0.0.1:3091/v1", {"reasoning_effort": "low"}, True),
        # api.openai.com enables reasoning by default, so tools alone bridge.
        ("https://api.openai.com/v1", {}, True),
        # Reasoning explicitly off keeps the request chat-servable.
        ("https://api.openai.com/v1", {"reasoning_effort": "none"}, False),
    ],
)
def test_responses_bridge_gate(base_url: str, extra: dict, bridged: bool) -> None:
    call = _bridged(base_url, tools=[TOOL], **extra)
    assert (call.wire == "responses") is bridged


def test_gpt54_without_tools_stays_on_chat() -> None:
    call = _bridged("https://api.openai.com/v1", reasoning_effort="high")
    assert call.wire == "openai"
    assert call.url == "https://api.openai.com/v1/chat/completions"


_FUNCTION_CALL = {
    "type": "function_call",
    "id": "fc_1",
    "call_id": "call_abcXYZ",
    "name": "t",
    "arguments": '{"a":1}',
}
_RESPONSES_STREAM = _sse(
    {"type": "response.created", "response": {"id": "r", "output": []}},
    {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"type": "reasoning", "id": "rs"},
    },
    {
        "type": "response.reasoning_summary_text.delta",
        "output_index": 0,
        "summary_index": 0,
        "delta": "plan",
    },
    {
        "type": "response.output_item.added",
        "output_index": 1,
        "item": {**_FUNCTION_CALL, "arguments": ""},
    },
    {
        "type": "response.function_call_arguments.delta",
        "output_index": 1,
        "delta": '{"a":',
    },
    {
        "type": "response.function_call_arguments.delta",
        "output_index": 1,
        "delta": "1}",
    },
    {"type": "response.output_item.done", "output_index": 1, "item": _FUNCTION_CALL},
    {
        "type": "response.completed",
        "response": {
            "id": "r",
            "output": [{"type": "reasoning", "id": "rs"}, _FUNCTION_CALL],
            "usage": {
                "input_tokens": 30,
                "output_tokens": 9,
                "input_tokens_details": {"cached_tokens": 10},
            },
        },
    },
)


@pytest.mark.asyncio
async def test_responses_stream_becomes_anthropic_events() -> None:
    events, _ = await _run(
        "openai/",
        "https://api.openai.com/v1",
        "gpt-5.4",
        _body(tools=[TOOL], stream=True),
        _respond(_RESPONSES_STREAM, headers={"content-type": "text/event-stream"}),
    )

    starts = [e["content_block"] for e in events if e["type"] == "content_block_start"]
    assert starts[0] == {"type": "thinking", "thinking": "", "signature": ""}
    assert starts[1] == {
        "type": "tool_use",
        "id": "call_abcXYZ",
        "name": "t",
        "input": {},
    }
    # litellm opened an empty text block for the trailing reasoning summary.
    assert starts[2] == {"type": "text", "text": ""}
    json_parts = [
        e["delta"]["partial_json"]
        for e in events
        if e["type"] == "content_block_delta"
        and e["delta"]["type"] == "input_json_delta"
    ]
    assert "".join(json_parts) == '{"a":1}'
    final = next(e for e in events if e["type"] == "message_delta")
    assert final["delta"] == {"stop_reason": "tool_use"}
    assert final["usage"] == {
        "input_tokens": 20,
        "output_tokens": 9,
        "cache_read_input_tokens": 10,
    }


@pytest.mark.asyncio
async def test_responses_failure_event_is_an_upstream_error() -> None:
    error, requests = await _run(
        "openai/",
        "https://api.openai.com/v1",
        "gpt-5.4",
        _body(tools=[TOOL]),
        _respond(
            _sse(
                {"type": "response.output_text.delta", "delta": "x"},
                {
                    "type": "response.failed",
                    "response": {
                        "error": {"code": "rate_limit_exceeded", "message": "slow"}
                    },
                },
            )
        ),
    )
    assert isinstance(error, UpstreamError)
    assert error.status_code == 429
    assert error.message == "Upstream error: slow"
    # The Responses bridge used litellm's httpx client: no SDK retries.
    assert len(requests) == 1


# --------------------------------------------------------------------------
# Stream details litellm's provider handlers added
# --------------------------------------------------------------------------


def _chat_chunk(delta: dict, finish: str | None = None, **extra: Any) -> dict:
    return {
        "id": "c",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **extra,
    }


@pytest.mark.asyncio
async def test_missing_usage_is_estimated_locally() -> None:
    message, _ = await _run(
        "openai/",
        "http://127.0.0.1:3091/v1",
        "gpt-4o",
        _body(),
        _respond(
            _sse(
                _chat_chunk({"role": "assistant", "content": "Hello world"}),
                _chat_chunk({}, "stop"),
            )
        ),
    )
    assert message["usage"]["input_tokens"] == 8
    assert message["usage"]["output_tokens"] == 2


@pytest.mark.parametrize(
    ("prefix", "base_url", "status"),
    [
        # openai-SDK providers surfaced every stream error payload as a 500.
        ("openai/", "http://127.0.0.1:3091/v1", 500),
        # litellm's own httpx handlers took the status from ``error.code``.
        ("groq/", "https://api.groq.com/openai/v1", 429),
    ],
)
@pytest.mark.asyncio
async def test_mid_stream_error_status(prefix: str, base_url: str, status: int) -> None:
    error, _ = await _run(
        prefix,
        base_url,
        "llama-3.3-70b-versatile",
        _body(),
        _respond(
            _sse(
                _chat_chunk({"role": "assistant", "content": "x"}),
                {"error": {"message": "bad thing", "code": 429}},
            )
        ),
    )
    assert isinstance(error, UpstreamError)
    assert error.status_code == status
    assert error.message == "Upstream error: bad thing"


@pytest.mark.asyncio
async def test_openrouter_reasoning_field_wins() -> None:
    events, _ = await _run(
        "openrouter/",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4o",
        _body(stream=True),
        _respond(
            _sse(
                # OpenRouter's parser overwrote reasoning_content with `reasoning`.
                _chat_chunk({"role": "assistant", "reasoning_content": "ignored"}),
                _chat_chunk({"reasoning": "used"}),
                _chat_chunk({"content": "answer"}),
                _chat_chunk(
                    {}, "stop", usage={"prompt_tokens": 3, "completion_tokens": 2}
                ),
            )
        ),
    )
    thinking = [
        e["delta"]["thinking"]
        for e in events
        if e["type"] == "content_block_delta" and e["delta"]["type"] == "thinking_delta"
    ]
    assert thinking == ["used"]


@pytest.mark.parametrize(
    ("prefix", "base_url", "attempts"),
    [
        ("openai/", "http://127.0.0.1:3091/v1", 3),
        ("together_ai/", "https://api.together.xyz/v1", 1),
    ],
)
@pytest.mark.asyncio
async def test_retries_follow_the_litellm_client(
    prefix: str, base_url: str, attempts: int
) -> None:
    error, requests = await _run(
        prefix,
        base_url,
        "llama-3.3-70b",
        _body(),
        _respond('{"error": {"message": "busy"}}', status=503),
    )
    assert isinstance(error, UpstreamError)
    assert error.status_code == 503
    assert len(requests) == attempts


# --------------------------------------------------------------------------
# Ollama /api/chat
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_ndjson_stream() -> None:
    lines = [
        {
            "model": "m",
            "message": {"role": "assistant", "content": "", "thinking": "hmm"},
            "done": False,
        },
        {
            "model": "m",
            "message": {"role": "assistant", "content": "Hi"},
            "done": False,
        },
        {
            "model": "m",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "length",
            "prompt_eval_count": 12,
            "eval_count": 3,
        },
    ]
    message, requests = await _run(
        "ollama_chat/",
        "http://localhost:11434",
        "qwen3:8b",
        _body(reasoning_effort="high", top_k=4),
        _respond("\n".join(json.dumps(line) for line in lines) + "\n"),
    )

    sent = json.loads(requests[0].content)
    assert str(requests[0].url) == "http://localhost:11434/api/chat"
    assert sent["think"] is True
    assert sent["options"] == {"num_predict": 300, "top_k": 4}
    assert sent["messages"] == [{"role": "user", "content": "hi", "images": []}]
    assert message["content"] == [
        {"type": "thinking", "thinking": "hmm", "signature": ""},
        {"type": "text", "text": "Hi"},
    ]
    assert message["stop_reason"] == "max_tokens"
    assert message["usage"]["input_tokens"] == 12
    assert message["usage"]["output_tokens"] == 3


# --------------------------------------------------------------------------
# Review follow-ups
# --------------------------------------------------------------------------


def _usage_chunk(usage: dict) -> dict:
    return {"id": "c", "choices": [], "usage": usage}


_XAI_USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 50,
    "total_tokens": 1450,
    "prompt_tokens_details": {"cached_tokens": 100},
    "completion_tokens_details": {"reasoning_tokens": 400},
}


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize(
    ("total", "output"),
    [
        # xAI leaves reasoning out of completion_tokens but in the total.
        (1450, 450),
        # Totals that already add up mean reasoning is included: no fold.
        (1050, 50),
    ],
)
@pytest.mark.asyncio
async def test_xai_reasoning_tokens_are_billed(
    stream: bool, total: int, output: int
) -> None:
    result, _ = await _run(
        "xai/",
        "https://api.x.ai/v1",
        "grok-4",
        _body(stream=stream),
        _respond(
            _sse(
                _chat_chunk({"role": "assistant", "content": "ok"}),
                _chat_chunk({}, "stop"),
                _usage_chunk({**_XAI_USAGE, "total_tokens": total}),
            )
        ),
    )
    usage = (
        next(e for e in result if e["type"] == "message_delta")["usage"]
        if stream
        else result["usage"]
    )
    assert usage["input_tokens"] == 900
    assert usage["cache_read_input_tokens"] == 100
    assert usage["output_tokens"] == output


@pytest.mark.asyncio
async def test_numeric_string_usage_is_billed() -> None:
    message, _ = await _run(
        "openai/",
        "http://127.0.0.1:3091/v1",
        "gpt-4o",
        _body(),
        _respond(
            _sse(
                _chat_chunk({"role": "assistant", "content": "ok"}),
                _chat_chunk({}, "stop"),
                _usage_chunk({"prompt_tokens": "1000", "completion_tokens": "50"}),
            )
        ),
    )
    assert message["usage"]["input_tokens"] == 1000
    assert message["usage"]["output_tokens"] == 50


@pytest.mark.asyncio
async def test_venice_nested_cache_writes_are_billed_as_cache_writes() -> None:
    message, _ = await _run(
        "openai/",
        "https://api.venice.ai/api/v1",
        "claude-sonnet-4-5",
        _body(),
        _respond(
            _sse(
                _chat_chunk({"role": "assistant", "content": "ok"}),
                _chat_chunk({}, "stop"),
                _usage_chunk(
                    {
                        "prompt_tokens": 1000,
                        "completion_tokens": 40,
                        "prompt_tokens_details": {
                            "cached_tokens": 200,
                            "cache_creation_input_tokens": 300,
                        },
                    }
                ),
            )
        ),
    )
    assert message["usage"]["input_tokens"] == 500
    assert message["usage"]["cache_read_input_tokens"] == 200
    assert message["usage"]["cache_creation_input_tokens"] == 300


@pytest.mark.asyncio
async def test_non_json_stream_event_is_an_error_not_a_gap() -> None:
    error, _ = await _run(
        "openai/",
        "http://127.0.0.1:3091/v1",
        "gpt-4o",
        _body(),
        _respond(
            _sse(_chat_chunk({"role": "assistant", "content": "partial"}))
            + "data: Internal Server Error\n\n"
        ),
    )
    assert isinstance(error, UpstreamError)
    assert error.status_code == 500
    assert "Malformed upstream event: Internal Server Error" in error.message


def _call(index: int, call_id: str, name: str, arguments: str) -> dict:
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


@pytest.mark.asyncio
async def test_parallel_tool_calls_in_one_delta_get_their_own_blocks() -> None:
    message, _ = await _run(
        "openai/",
        "http://127.0.0.1:3091/v1",
        "gpt-4o",
        _body(tools=[TOOL]),
        _respond(
            _sse(
                _chat_chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            _call(0, "call_a", "Read", '{"path": "a.py"}'),
                            _call(1, "call_b", "Bash", '{"cmd": "ls"}'),
                        ],
                    }
                ),
                _chat_chunk({}, "tool_calls"),
            )
        ),
    )
    assert [(b["id"], b["name"], b["input"]) for b in message["content"]] == [
        ("call_a", "Read", {"path": "a.py"}),
        ("call_b", "Bash", {"cmd": "ls"}),
    ]
    assert message["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_ollama_parallel_tool_calls_stay_separate() -> None:
    line = {
        "model": "m",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "Read", "arguments": {"path": "a.py"}}},
                {"function": {"name": "Bash", "arguments": {"cmd": "ls"}}},
            ],
        },
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 5,
        "eval_count": 3,
    }
    message, _ = await _run(
        "ollama_chat/",
        "http://localhost:11434",
        "qwen3:8b",
        _body(tools=[TOOL]),
        _respond(json.dumps(line) + "\n"),
    )
    assert [(b["name"], b["input"]) for b in message["content"]] == [
        ("Read", {"path": "a.py"}),
        ("Bash", {"cmd": "ls"}),
    ]


def test_missing_usage_estimate_counts_serialized_tool_calls() -> None:
    texts: list[str] = []

    def estimate(text: str) -> tuple[int, int]:
        texts.append(text)
        return 7, 3

    normalizer = ChatStreamNormalizer(estimate_usage=estimate)
    for chunk in (
        _chat_chunk({"role": "assistant", "content": "On it."}),
        _chat_chunk({"tool_calls": [_call(0, "call_1", "Read", '{"path": ')]}),
        _chat_chunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]}
        ),
        _chat_chunk({}, "tool_calls"),
    ):
        normalizer.feed(chunk)
    usage = normalizer.usage()

    # litellm counted each merged call as ``ChatCompletionMessageToolCall``
    # JSON, not just its arguments.
    assert texts == [
        'On it.{"function":{"arguments":"{\\"path\\": \\"a.py\\"}","name":"Read"},'
        '"id":"call_1","type":"function"}'
    ]
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (7, 3)


@pytest.mark.parametrize(
    ("prefix", "base_url"),
    [
        ("deepseek/", "https://api.deepseek.com/anthropic"),
        ("groq/", "https://api.groq.com/openai/v1"),
    ],
)
@pytest.mark.asyncio
async def test_deeply_nested_tool_schema_is_a_client_error(
    prefix: str, base_url: str
) -> None:
    schema: dict[str, Any] = {"type": "string"}
    # Past the old deepcopy recursion limit, within json's on every Python.
    for _ in range(300):
        schema = {"type": "object", "properties": {"a": schema}}
    error, _ = await _run(
        prefix,
        base_url,
        "m",
        _body(tools=[{**TOOL, "input_schema": schema}]),
        _respond('{"error": {"message": "schema too deep"}}', status=400),
    )
    assert isinstance(error, UpstreamError)
    assert error.status_code == 400


# --------------------------------------------------------------------------
# litellm bugs fixed on purpose (REQUEST_FIXES in the parity test)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "base_url", "url"),
    [
        (
            "anthropic/",
            "https://api.anthropic.com/v1",
            "https://api.anthropic.com/v1/messages",
        ),
        (
            "anthropic/",
            "https://api.anthropic.com/v1/",
            "https://api.anthropic.com/v1/messages",
        ),
        (
            "anthropic/",
            "https://api.anthropic.com",
            "https://api.anthropic.com/v1/messages",
        ),
        ("minimax/", "https://api.minimax.io/v1", "https://api.minimax.io/v1/messages"),
        (
            "minimax/",
            "https://api.minimax.io/anthropic/",
            "https://api.minimax.io/anthropic/v1/messages",
        ),
        (
            "minimax/",
            "https://api.minimax.io/anthropic/v1/messages",
            "https://api.minimax.io/anthropic/v1/messages",
        ),
    ],
)
def test_native_messages_url_never_doubles_v1(
    prefix: str, base_url: str, url: str
) -> None:
    call = build_upstream_call(
        prefix=prefix,
        base_url=base_url,
        api_key="k",
        upstream_model="m",
        body=_body(),
    )
    assert call.url == url


@pytest.mark.parametrize(
    ("api_version", "query"),
    [("2024-10-21", "2024-10-21"), (None, "2025-02-01-preview")],
)
def test_azure_chat_url_uses_the_configured_api_version(
    api_version: str | None, query: str
) -> None:
    call = build_upstream_call(
        prefix="azure/",
        base_url="https://res.openai.azure.com",
        api_key="k",
        upstream_model="gpt-4o",
        body=_body(),
        api_version=api_version,
    )
    assert call.url == (
        "https://res.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
        f"?api-version={query}"
    )


@pytest.mark.parametrize(
    ("row_version", "query"),
    [
        ("2024-10-21", "2024-10-21"),
        ("\ufeff 2024-06-01 ", "2024-06-01"),
        ("v1", "2025-02-01-preview"),
        ("", "2025-02-01-preview"),
    ],
)
@pytest.mark.asyncio
async def test_azure_row_api_version_reaches_messages_call(
    row_version: str, query: str
) -> None:
    provider = AzureUpstreamProvider(
        "https://res.openai.azure.com", "k", api_version=row_version
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=_sse(
                _chat_chunk({"role": "assistant", "content": "ok"}),
                _chat_chunk(
                    {}, "stop", usage={"prompt_tokens": 3, "completion_tokens": 1}
                ),
            ),
        )

    with respx.mock() as router:
        router.route().mock(side_effect=handler)
        await provider._dispatch_anthropic_messages(
            json.dumps(_body()).encode(), _model("gpt-4o")
        )
    assert requests[0].url.params["api-version"] == query


def test_mistral_reasoning_prompt_says_boxed() -> None:
    call = build_upstream_call(
        prefix="mistral/",
        base_url="https://api.mistral.ai/v1",
        api_key="k",
        upstream_model="magistral-medium-latest",
        body=_body(reasoning_effort="medium"),
    )
    system = call.body["messages"][0]["content"]
    assert "NEVER use \\boxed{} in your response." in system
    assert "\b" not in system


def _tool_events(events: list[dict]) -> list[tuple[str, int, str]]:
    """(type, index, payload) per block event, asserting strict block order."""
    out: list[tuple[str, int, str]] = []
    open_index: int | None = None
    for event in events:
        kind = event["type"]
        if kind == "content_block_start":
            assert open_index is None
            open_index = event["index"]
            out.append((kind, open_index, event["content_block"].get("name", "")))
        elif kind == "content_block_delta":
            assert event["index"] == open_index
            out.append((kind, open_index, event["delta"].get("partial_json", "")))
        elif kind == "content_block_stop":
            assert event["index"] == open_index
            out.append((kind, open_index, ""))
            open_index = None
    return out


def test_interleaved_tool_fragments_stay_with_their_call() -> None:
    translator = AnthropicEventTranslator("m")
    translator.start()

    def feed(*calls: dict) -> list[dict]:
        return translator.feed(
            {"choices": [{"delta": {"tool_calls": list(calls)}, "finish_reason": None}]}
        )

    first = feed(_call(0, "call_w", "get_weather", "")) + feed(
        {"index": 0, "function": {"arguments": '{"city": '}}
    )
    # The first call streams live.
    assert _tool_events(first)[-1] == ("content_block_delta", 0, '{"city": ')
    # A second call and its fragments are held while index 0 is still open...
    held = feed(_call(1, "call_t", "get_time", '{"tz"'))
    held += feed({"index": 0, "function": {"arguments": '"Paris"}'}})
    held += feed({"index": 1, "function": {"arguments": ': "UTC"}'}})
    assert [e for e in held if e["type"] != "content_block_delta"] == []
    assert [e["delta"]["partial_json"] for e in held] == ['"Paris"}']
    # ...and emitted whole once the stream finishes.
    rest = translator.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    rest += translator.finish()

    assert _tool_events(first + held + rest) == [
        ("content_block_start", 0, "get_weather"),
        ("content_block_delta", 0, '{"city": '),
        ("content_block_delta", 0, '"Paris"}'),
        ("content_block_stop", 0, ""),
        ("content_block_start", 1, "get_time"),
        ("content_block_delta", 1, '{"tz": "UTC"}'),
        ("content_block_stop", 1, ""),
    ]


@pytest.mark.asyncio
async def test_text_after_held_tool_calls_keeps_block_order() -> None:
    message, _ = await _run(
        "openai/",
        "http://127.0.0.1:3091/v1",
        "gpt-4o",
        _body(tools=[TOOL]),
        _respond(
            _sse(
                _chat_chunk(
                    {"role": "assistant", "tool_calls": [_call(0, "call_a", "a", "{}")]}
                ),
                _chat_chunk({"tool_calls": [_call(1, "call_b", "b", '{"x":')]}),
                _chat_chunk(
                    {"tool_calls": [{"index": 1, "function": {"arguments": "1}"}}]}
                ),
                _chat_chunk({"content": "done"}),
                _chat_chunk({}, "stop"),
            )
        ),
    )
    assert [
        (b["type"], b.get("name"), b.get("input"), b.get("text"))
        for b in message["content"]
    ] == [
        ("tool_use", "a", {}, None),
        ("tool_use", "b", {"x": 1}, None),
        ("text", None, None, "done"),
    ]
