"""Parity of the native ``/v1/messages`` bridge with litellm 1.101.2.

``tests/fixtures/litellm_oracle/`` records what litellm put on the wire and
emitted back for each case (see ``scripts/capture_litellm_oracle.py`` for how
and for the normalisation rules N1-N5 applied here). Every case must match;
deliberate divergences are listed in ``DIVERGENCES`` with the reason, and
litellm bugs the native bridge fixes in ``REQUEST_FIXES``: those rewrite only
the faulty field of the recorded request, so the rest is still compared.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from routstr.core.exceptions import UpstreamError
from routstr.payment.models import Architecture, Model, Pricing, Reasoning
from routstr.upstream import messages_transport
from routstr.upstream.anthropic_openai import anthropic_to_openai
from routstr.upstream.azure import AzureUpstreamProvider
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.deepseek import DeepSeekUpstreamProvider
from routstr.upstream.fireworks import FireworksUpstreamProvider
from routstr.upstream.generic import GenericUpstreamProvider
from routstr.upstream.groq import GroqUpstreamProvider
from routstr.upstream.messages_dispatch import events_from_chunk
from routstr.upstream.ollama import OllamaUpstreamProvider
from routstr.upstream.openai import OpenAIUpstreamProvider
from routstr.upstream.perplexity import PerplexityUpstreamProvider
from routstr.upstream.together import TogetherUpstreamProvider
from routstr.upstream.venice import VeniceUpstreamProvider
from routstr.upstream.xai import XAIUpstreamProvider

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "litellm_oracle"
API_KEY = "sk-test"

# Case id (``provider/case_id``) -> why the native bridge differs on purpose.
DIVERGENCES = {
    "generic-local__error_object_mid_stream": (
        "litellm raised its own MidStreamFallbackError mid-stream; the native "
        "bridge raises StreamPayloadError, which the streaming paths map to the "
        "same UpstreamError(500)"
    ),
}


def _fix_versioned_messages_url(expected: dict[str, Any]) -> dict[str, Any]:
    # A ``.../v1`` base became ``.../v1/v1/messages``, which 404s.
    assert expected["url"].endswith("/v1/v1/messages"), expected["url"]
    return {**expected, "url": expected["url"].replace("/v1/v1/", "/v1/", 1)}


def _fix_azure_api_version(expected: dict[str, Any]) -> dict[str, Any]:
    # litellm pinned its own default and ignored the row (2024-10-21 here).
    url = expected["url"]
    assert url.endswith("api-version=2025-02-01-preview"), url
    return {**expected, "url": url.replace("2025-02-01-preview", "2024-10-21")}


_BACKSPACE_BOXED = "\x08oxed"  # what litellm sent for "\\boxed"


def _replace_text(obj: Any, old: str, new: str) -> Any:
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, dict):
        return {k: _replace_text(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_text(v, old, new) for v in obj]
    return obj


def _fix_mistral_backspace(expected: dict[str, Any]) -> dict[str, Any]:
    # The non-raw prompt string turned ``\\boxed`` into BACKSPACE + "oxed".
    return {
        **expected,
        "json": _replace_text(expected["json"], _BACKSPACE_BOXED, "\\boxed"),
    }


# Provider row -> (why the recorded litellm request was wrong, how to fix it,
# how many recorded cases the fix rewrites). test_request_fixes_are_explicit
# pins those counts so a recapture cannot silently widen or drop a fix.
REQUEST_FIXES: dict[str, tuple[str, Any, int]] = {
    "generic-anthropic-host": (
        "base_url https://api.anthropic.com/v1 was sent to /v1/v1/messages",
        _fix_versioned_messages_url,
        22,
    ),
    "generic-minimax": (
        "base_url https://api.minimax.io/v1 was sent to /v1/v1/messages",
        _fix_versioned_messages_url,
        22,
    ),
    "azure": (
        "the provider row's api_version is used; litellm always sent "
        "2025-02-01-preview (now only the default when the row has none)",
        _fix_azure_api_version,
        31,
    ),
    "generic-mistral": (
        "the reasoning system prompt says 'NEVER use \\boxed{}'; litellm's "
        "non-raw string sent a backspace character instead (only cases that "
        "inject the prompt change)",
        _fix_mistral_backspace,
        3,
    ),
}
_SPLIT_ARGS_CALLS = [
    ("call_w1", "get_weather", {"city": "Paris"}),
    ("call_t2", "get_time", {"tz": "UTC"}),
]
# Response case -> (why litellm's output was wrong, the tool calls it should
# have produced). Content blocks are checked against the calls; everything
# else in the events and aggregate is still compared with the recording.
RESPONSE_FIXES: dict[str, tuple[str, list[tuple[str, str, dict]]]] = {
    case_id: (
        "argument fragments for index 0 arrive after index 1 opened; litellm "
        "appended them to the open block (get_time), the native bridge keeps "
        "each call's fragments together",
        _SPLIT_ARGS_CALLS,
    )
    for case_id in (
        "generic-local__parallel_tool_calls_split_args",
        "venice__parallel_tool_calls_split_args",
    )
}
# Applied to every case.
GLOBAL_DIVERGENCES = (
    "Only behavioural headers are compared (auth, api-key, x-api-key, "
    "anthropic-version, content-type): user-agent, accept and the openai SDK's "
    "x-stainless-* headers were client artefacts.",
    "Ollama's /api/show capability lookups (aux_requests) are not made: the "
    "request body never depended on them.",
    "Error messages read 'Upstream error: <text>' instead of 'Upstream error "
    "via litellm: <Provider>Exception - <text>'.",
    "429 details also carry retry_after_seconds parsed from Retry-After, which "
    "litellm's exception never passed to classify_rate_limit.",
)
_COMPARED_HEADERS = frozenset(
    {"authorization", "api-key", "x-api-key", "anthropic-version", "content-type"}
)
_ID_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"msg_[A-Za-z0-9_\-]+",
        r"toolu_[A-Za-z0-9_\-]+",
        r"call_[A-Za-z0-9_\-]+",
        r"chatcmpl-[A-Za-z0-9_\-]+",
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    )
)

OPENAI_SSE = (
    'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":'
    '{"role":"assistant","content":"Hi"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{},'
    '"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\n'
    "data: [DONE]\n\n"
)
ANTHROPIC_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",'
    '"type":"message","role":"assistant","content":[],"model":"x",'
    '"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
OLLAMA_NDJSON = (
    '{"model":"m","message":{"role":"assistant","content":"Hi"},"done":false}\n'
    '{"model":"m","message":{"role":"assistant","content":""},"done":true,'
    '"done_reason":"stop","prompt_eval_count":5,"eval_count":1}\n'
)
# The body ``capture_responses``/``capture_errors`` sent with every case.
RESPONSE_REQUEST_BODY = {
    "max_tokens": 512,
    "messages": [{"role": "user", "content": "Hi"}],
    "tools": [
        {
            "name": "get_weather",
            "description": "Get the current weather for a city",
            "input_schema": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                    "unit": {"type": "string", "enum": ["c", "f"]},
                },
                "required": ["city"],
            },
        },
        {
            "name": "get_time",
            "description": "Current time in a timezone",
            "input_schema": {
                "type": "object",
                "properties": {"tz": {"type": "string"}},
            },
        },
    ],
}
# ``responses.json``/``errors.json`` name providers but not their rows.
PROVIDER_ROWS = {
    "generic-local": ("GenericUpstreamProvider", "http://127.0.0.1:3091/v1"),
    "venice": ("VeniceUpstreamProvider", None),
    "deepseek": ("DeepSeekUpstreamProvider", None),
    "generic-minimax": ("GenericUpstreamProvider", "https://api.minimax.io/v1"),
    "generic-openrouter": ("GenericUpstreamProvider", "https://openrouter.ai/api/v1"),
}


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _registry_note() -> str:
    meta = _load("meta.json")
    data = (ROOT / meta["model_cost_file"]).read_bytes()
    if hashlib.sha256(data).hexdigest() == meta["model_cost_sha256"]:
        return ""
    return (
        f" NOTE: {meta['model_cost_file']} changed since the fixtures were "
        "captured; registry-dependent param decisions may legitimately differ."
    )


def _strings(obj: Any, out: set[str] | None = None) -> set[str]:
    out = set() if out is None else out
    if isinstance(obj, str):
        out.add(obj)
        if obj[:1] in "{[":
            try:
                _strings(json.loads(obj), out)
            except ValueError:
                pass
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _strings(key, out)
            _strings(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _strings(item, out)
    return out


def _normalise(obj: Any, keep: set[str]) -> Any:
    """Rule N1: mask generated ids that are not in the case input."""
    if isinstance(obj, str):
        if obj not in keep and any(p.fullmatch(obj) for p in _ID_PATTERNS):
            return "<id>"
        return obj
    if isinstance(obj, dict):
        return {k: _normalise(v, keep) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalise(v, keep) for v in obj]
    return obj


def _headers(headers: Any) -> dict[str, str]:
    """Rule N2 on the compared headers."""
    out = {}
    for name, value in headers.items():
        name = name.lower()
        if name not in _COMPARED_HEADERS:
            continue
        if name in ("authorization", "api-key", "x-api-key"):
            value = value.replace(API_KEY, "<redacted>")
        out[name] = value
    return out


def _model(model_id: str, reasoning: dict | None = None) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        forwarded_model_id=model_id,
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
        pricing=Pricing(
            prompt=0.0,
            completion=0.0,
            request=0.0,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
            max_cost=0.0,
        ),
        sats_pricing=None,
        per_request_limits=None,
        top_provider=None,
        reasoning=Reasoning(**reasoning) if reasoning else None,
    )


def _provider(
    cls_name: str, base_url: str | None, blank_key: bool
) -> BaseUpstreamProvider:
    key = "" if blank_key else API_KEY
    by_key: dict[str, Any] = {
        "OpenAIUpstreamProvider": OpenAIUpstreamProvider,
        "VeniceUpstreamProvider": VeniceUpstreamProvider,
        "DeepSeekUpstreamProvider": DeepSeekUpstreamProvider,
        "TogetherUpstreamProvider": TogetherUpstreamProvider,
        "GroqUpstreamProvider": GroqUpstreamProvider,
        "FireworksUpstreamProvider": FireworksUpstreamProvider,
        "XAIUpstreamProvider": XAIUpstreamProvider,
        "PerplexityUpstreamProvider": PerplexityUpstreamProvider,
    }
    if cls_name in by_key:
        return by_key[cls_name](key)
    assert base_url is not None
    if cls_name == "AzureUpstreamProvider":
        return AzureUpstreamProvider(base_url, key, api_version="2024-10-21")
    if cls_name == "OllamaUpstreamProvider":
        return OllamaUpstreamProvider(base_url)
    assert cls_name == "GenericUpstreamProvider", cls_name
    return GenericUpstreamProvider(base_url, key)


def _default_response(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/messages"):
        body, kind = ANTHROPIC_SSE, "text/event-stream"
    elif path.endswith("/api/chat"):
        body, kind = OLLAMA_NDJSON, "application/x-ndjson"
    else:
        body, kind = OPENAI_SSE, "text/event-stream"
    return httpx.Response(200, headers={"content-type": kind}, content=body.encode())


async def _dispatch(
    provider: BaseUpstreamProvider,
    model_obj: Model,
    body: dict[str, Any],
    responder: Any,
) -> dict[str, Any]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responder(request)

    outcome: dict[str, Any] = {"requests": requests}
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route().mock(side_effect=handler)
        try:
            client_stream, result, _ = await provider._dispatch_anthropic_messages(
                json.dumps(body).encode(), model_obj
            )
        except UpstreamError as exc:
            outcome["error"] = exc
            return outcome
        if not client_stream:
            outcome["aggregate"] = result
            return outcome
        events: list[dict] = []
        buffer = b""
        try:
            async for chunk in result:
                parsed, buffer = events_from_chunk(chunk, buffer)
                events.extend(parsed)
        except Exception as exc:  # recorded, compared by the caller
            outcome["stream_exception"] = exc
        outcome["events"] = events
    return outcome


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(messages_transport, "_retry_delay", lambda *_: 0.0)


def _upstream_message(message: str) -> str:
    """Strip the wrapper both implementations put around the upstream text."""
    message = re.sub(r"^Upstream error(?: via litellm)?: ", "", message)
    return re.sub(r"^\w+Exception - ", "", message)


def _assert_error(exc: UpstreamError, expected: dict[str, Any]) -> None:
    assert exc.status_code == expected["status_code"]
    assert exc.code == expected["code"]
    assert exc.from_upstream_response == expected.get("from_upstream_response", False)
    assert _upstream_message(exc.message) == _upstream_message(expected["message"])
    assert _details(exc.details) == _details(expected["details"])


def _details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return None
    out = {k: v for k, v in details.items() if k != "retry_after_seconds"}
    if "message" in out:
        out["message"] = _upstream_message(out["message"])
    return out


# --------------------------------------------------------------------------
# A. Request side
# --------------------------------------------------------------------------

_REQUEST_CASES = _load("requests.json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    _REQUEST_CASES,
    ids=[f"{c['provider']}/{c['case_id']}/{c['model_id']}" for c in _REQUEST_CASES],
)
async def test_request_matches_oracle(case: dict[str, Any]) -> None:
    provider = _provider(
        case["provider_class"], case["base_url"], case["api_key_blank"]
    )
    assert provider.get_provider_prefix() == case["provider_prefix"]
    outcome = await _dispatch(
        provider,
        _model(case["model_id"], case["model_reasoning"]),
        case["input_body"],
        _default_response,
    )
    expected = case["expected"]
    if "error" not in expected:
        expected = _fixed_request(case["provider"], expected)
    note = _registry_note()
    host = httpx.URL(case["base_url"]).host
    sent = [r for r in outcome["requests"] if r.url.host == host]

    if "error" in expected:
        assert "error" in outcome, f"expected {expected['error']}{note}"
        assert not sent
        _assert_error(outcome["error"], expected["error"])
        return

    assert "error" not in outcome, f"{outcome.get('error')!r}{note}"
    assert len(sent) == expected["request_count"]
    request = sent[0]
    keep = _strings(case["input_body"])
    assert request.method == expected["method"]
    assert str(request.url) == expected["url"]
    assert _headers(request.headers) == _headers(expected["headers"])
    assert _normalise(json.loads(request.content), keep) == expected["json"], note


def _fixed_request(provider: str, expected: dict[str, Any]) -> dict[str, Any]:
    if provider in REQUEST_FIXES:
        return REQUEST_FIXES[provider][1](expected)
    return expected


def test_request_fixes_are_explicit() -> None:
    changed: dict[str, int] = {}
    for case in _REQUEST_CASES:
        expected = case["expected"]
        if (
            "error" not in expected
            and _fixed_request(case["provider"], expected) != expected
        ):
            changed[case["provider"]] = changed.get(case["provider"], 0) + 1
    assert changed == {row: fix[2] for row, fix in REQUEST_FIXES.items()}


# --------------------------------------------------------------------------
# A2. Adapter translation (gemini_messages)
# --------------------------------------------------------------------------

_ADAPTER_CASES = _load("adapter_translate.json")


@pytest.mark.parametrize(
    "case",
    _ADAPTER_CASES,
    ids=[f"{c['case_id']}/{c['model']}" for c in _ADAPTER_CASES],
)
def test_adapter_translation_matches_oracle(case: dict[str, Any]) -> None:
    translated, _ = anthropic_to_openai(
        {"model": case["model"], **case["input_body"]}, prompt_cache_key=False
    )
    assert translated == case["expected_openai_kwargs"]


# --------------------------------------------------------------------------
# B. Response side
# --------------------------------------------------------------------------

_RESPONSE_CASES = _load("responses.json")


def _responder(upstream: dict[str, Any]) -> Any:
    raw = "\n".join(upstream["sse_lines"]).encode()

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            upstream["status"], headers=upstream["headers"], content=raw
        )

    return respond


def _payload_strings(upstream: dict[str, Any]) -> set[str]:
    payloads = []
    for line in upstream["sse_lines"]:
        if line.startswith("data:"):
            try:
                payloads.append(json.loads(line[5:]))
            except ValueError:
                pass
    return _strings(payloads)


def _streamed_tool_calls(events: list[dict]) -> list[tuple[str, str, dict]]:
    """Tool calls of a strictly sequential Anthropic event stream."""
    calls: list[tuple[str, str, dict]] = []
    open_index: int | None = None
    block: dict[str, Any] = {}
    parts: list[str] = []
    for event in events:
        kind = event["type"]
        if kind == "content_block_start":
            assert open_index is None, "block opened before the previous stopped"
            open_index, block, parts = event["index"], event["content_block"], []
        elif kind == "content_block_delta":
            assert event["index"] == open_index, "delta for a block that is not open"
            parts.append(event["delta"].get("partial_json", ""))
        elif kind == "content_block_stop":
            assert event["index"] == open_index
            if block["type"] == "tool_use":
                calls.append((block["id"], block["name"], json.loads("".join(parts))))
            open_index = None
    return calls


async def _assert_fixed_response(
    case: dict[str, Any],
    provider: BaseUpstreamProvider,
    model_obj: Model,
    streamed: dict[str, Any],
    responder: Any,
) -> None:
    calls = RESPONSE_FIXES[case["case_id"]][1]
    recorded = [
        (b["id"], b["name"], b["input"]) for b in case["expected_aggregate"]["content"]
    ]
    assert recorded != calls, "the recording no longer shows the litellm bug"

    def outside_blocks(events: list[dict]) -> list[str]:
        return [e["type"] for e in events if not e["type"].startswith("content_block")]

    assert _streamed_tool_calls(streamed["events"]) == calls
    assert outside_blocks(streamed["events"]) == outside_blocks(case["expected_events"])
    aggregated = await _dispatch(
        provider, model_obj, dict(RESPONSE_REQUEST_BODY), responder
    )
    message = aggregated["aggregate"]
    assert [(b["id"], b["name"], b["input"]) for b in message["content"]] == calls
    expected = case["expected_aggregate"]
    for key in ("stop_reason", "usage", "role", "type"):
        assert message[key] == expected[key]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", _RESPONSE_CASES, ids=[c["case_id"] for c in _RESPONSE_CASES]
)
async def test_response_matches_oracle(case: dict[str, Any]) -> None:
    cls_name, base_url = PROVIDER_ROWS[case["provider"]]
    provider = _provider(cls_name, base_url, blank_key=False)
    model_obj = _model(case["requested_model"])
    keep = _payload_strings(case["upstream"])
    responder = _responder(case["upstream"])

    streamed = await _dispatch(
        provider, model_obj, {**RESPONSE_REQUEST_BODY, "stream": True}, responder
    )
    assert "error" not in streamed
    if case["case_id"] in RESPONSE_FIXES:
        await _assert_fixed_response(case, provider, model_obj, streamed, responder)
        return
    assert _normalise(streamed["events"], keep) == case["expected_events"]
    if "expected_stream_exception" in case:
        assert case["case_id"] in DIVERGENCES
        assert "stream_exception" in streamed
    else:
        assert "stream_exception" not in streamed

    aggregated = await _dispatch(
        provider, model_obj, dict(RESPONSE_REQUEST_BODY), responder
    )
    if case.get("expected_aggregate_error"):
        _assert_error(aggregated["error"], case["expected_aggregate_error"])
    else:
        assert _normalise(aggregated["aggregate"], keep) == case["expected_aggregate"]


# --------------------------------------------------------------------------
# B2. Upstream HTTP errors
# --------------------------------------------------------------------------

_ERROR_CASES = _load("errors.json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream", [False, True], ids=["client_non_stream", "client_stream"]
)
@pytest.mark.parametrize("case", _ERROR_CASES, ids=[c["case_id"] for c in _ERROR_CASES])
async def test_upstream_error_matches_oracle(
    case: dict[str, Any], stream: bool
) -> None:
    cls_name, base_url = PROVIDER_ROWS[case["provider"]]
    provider = _provider(cls_name, base_url, blank_key=False)
    upstream = case["upstream"]
    body = upstream["body"]
    content = body.encode() if isinstance(body, str) else json.dumps(body).encode()

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            upstream["status"], headers=upstream["headers"], content=content
        )

    outcome = await _dispatch(
        provider,
        _model(case["requested_model"]),
        {**RESPONSE_REQUEST_BODY, "stream": stream},
        respond,
    )
    expected = case["client_stream" if stream else "client_non_stream"]
    assert len(outcome["requests"]) == expected["attempts"]
    _assert_error(outcome["error"], expected["error"])
