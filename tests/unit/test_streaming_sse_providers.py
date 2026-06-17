"""Battle-test the streaming SSE parser against real per-provider framing.

Each test drives the *actual* ``handle_streaming_chat_completion`` generator
with a mock upstream response whose ``aiter_bytes`` emits byte sequences that
mirror what each supported provider sends on the wire (captured from the
providers' own streaming docs):

* OpenAI / Groq / Fireworks / xAI / Perplexity / Azure - plain
  ``data: {json}\\n\\n`` + ``data: [DONE]``.
* OpenRouter - same, but with ``: OPENROUTER PROCESSING`` keepalive comments
  interleaved (the framing that produced the original
  ``Unexpected token ':'`` client crash).
* Gemini (native ``alt=sse``) - ``data:`` payloads framed with CRLF.

The invariant every provider must satisfy: every line the proxy emits that
starts with ``data: `` either equals ``[DONE]`` or is valid JSON, and no SSE
comment ever reaches the client. That invariant is exactly what the buggy
``re.split(b"data: ")`` parser violated for OpenRouter.
"""

import json
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from routstr.core.db import ApiKey
from routstr.upstream import base
from routstr.upstream.base import BaseUpstreamProvider


def _make_response(chunks: list[bytes]) -> MagicMock:
    async def aiter_bytes() -> AsyncGenerator[bytes, None]:
        for chunk in chunks:
            yield chunk

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/event-stream"}
    mock_response.aiter_bytes = aiter_bytes
    return mock_response


async def _drive(chunks: list[bytes], requested_model: str | None = None) -> list[bytes]:
    """Run the real streaming generator over ``chunks`` and collect output bytes."""
    provider = BaseUpstreamProvider(
        base_url="https://api.example.com", api_key="test_key"
    )

    key = MagicMock(spec=ApiKey)
    key.hashed_key = "test_hash"
    key.balance = 1000

    base.adjust_payment_for_tokens = AsyncMock(
        return_value={"total_usd": 0.1, "total_msats": 100}
    )
    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=key)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    base.create_session = MagicMock(return_value=mock_ctx)

    streaming_response = await provider.handle_streaming_chat_completion(
        response=_make_response(chunks),
        key=key,
        max_cost_for_model=100,
        background_tasks=MagicMock(),
        requested_model=requested_model,
    )

    out: list[bytes] = []
    async for chunk in streaming_response.body_iterator:
        if isinstance(chunk, str):
            out.append(chunk.encode())
        else:
            out.append(bytes(chunk))
    return out


def _data_payloads(out: list[bytes]) -> list[bytes]:
    """Return the raw payload of every ``data: `` line across all emitted bytes."""
    payloads: list[bytes] = []
    for chunk in out:
        for line in chunk.split(b"\n"):
            if line.startswith(b"data: "):
                payloads.append(line[len(b"data: ") :])
    return payloads


def _assert_clean(out: list[bytes]) -> list[dict]:
    """Core invariant: every data line is [DONE] or valid JSON; no comments leak."""
    blob = b"".join(out)
    # No SSE comment line must ever reach the client.
    for line in blob.split(b"\n"):
        assert not line.startswith(b":"), f"comment leaked to client: {line!r}"
        # The original bug signature: a data line whose value is itself a comment.
        assert not line.startswith(b"data: :"), f"mangled comment frame: {line!r}"

    objs: list[dict] = []
    for payload in _data_payloads(out):
        stripped = payload.strip()
        if stripped == b"[DONE]":
            continue
        obj = json.loads(stripped)  # raises if the proxy emitted non-JSON data
        objs.append(obj)
    return objs


@pytest.mark.asyncio
async def test_deepseek_usage_chunk_is_normalized_before_billing() -> None:
    """DeepSeek stream trailers keep raw fields and add canonical billing fields."""
    usage = {
        "prompt_tokens": 10000,
        "completion_tokens": 500,
        "total_tokens": 10500,
        "prompt_cache_hit_tokens": 9000,
        "prompt_cache_miss_tokens": 1000,
    }
    chunks = [
        b'data: {"id":"ds","model":"deepseek-chat","choices":[{"delta":{"content":"ok"}}]}\n\n',
        b"data: "
        + json.dumps(
            {
                "id": "ds",
                "model": "deepseek-chat",
                "choices": [],
                "usage": usage,
            }
        ).encode()
        + b"\n\n",
        b"data: [DONE]\n\n",
    ]

    await _drive(chunks)

    adjust_mock = cast(AsyncMock, base.adjust_payment_for_tokens)
    adjustment_input = adjust_mock.call_args.args[1]
    billed_usage = adjustment_input["usage"]
    assert billed_usage["prompt_tokens"] == 10000
    assert billed_usage["prompt_cache_hit_tokens"] == 9000
    assert billed_usage["prompt_cache_miss_tokens"] == 1000
    assert billed_usage["input_tokens"] == 1000
    assert billed_usage["output_tokens"] == 500
    assert billed_usage["cache_read_input_tokens"] == 9000
    assert billed_usage["cache_creation_input_tokens"] == 0


@pytest.mark.asyncio
async def test_fold_cache_tokens_does_not_double_count_inclusive_prompt_tokens() -> None:
    """Visible usage mutation must not inflate OpenAI-compatible prompt totals."""
    usage = {
        "prompt_tokens": 10000,
        "completion_tokens": 500,
        "cache_read_input_tokens": 9000,
        "prompt_cache_hit_tokens": 9000,
    }

    BaseUpstreamProvider._fold_cache_into_input_tokens(usage)

    assert usage["prompt_tokens"] == 10000
    assert usage["cache_read_input_tokens"] == 9000


@pytest.mark.asyncio
async def test_fold_cache_tokens_still_rolls_up_anthropic_input_tokens() -> None:
    """Anthropic native input_tokens excludes cache and still needs rollup."""
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 200,
    }

    BaseUpstreamProvider._fold_cache_into_input_tokens(usage)

    assert usage["input_tokens"] == 10200


@pytest.mark.asyncio
async def test_openai_style_plain_stream() -> None:
    """OpenAI / Groq / Fireworks / xAI / Perplexity: plain data + [DONE]."""
    chunks = [
        b'data: {"id":"x","choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"id":"x","choices":[{"delta":{"content":" world"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = await _drive(chunks)
    objs = _assert_clean(out)
    assert any(o.get("choices") for o in objs)
    assert b"data: [DONE]\n\n" in b"".join(out)


@pytest.mark.asyncio
async def test_openrouter_keepalive_comments() -> None:
    """OpenRouter ``: OPENROUTER PROCESSING`` keepalives must never crash clients.

    This is the exact regression: the old parser emitted
    ``data: : OPENROUTER PROCESSING`` which made downstream
    ``JSON.parse`` throw ``Unexpected token ':'``.
    """
    chunks = [
        b": OPENROUTER PROCESSING\n\n",
        b": OPENROUTER PROCESSING\n\n",
        b'data: {"id":"x","choices":[{"delta":{"content":"Hi"}}]}\n\n',
        b": OPENROUTER PROCESSING\n\n",
        b'data: {"id":"x","choices":[{"delta":{"content":"!"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = await _drive(chunks)
    objs = _assert_clean(out)
    # The keepalive must be gone entirely.
    assert b"OPENROUTER PROCESSING" not in b"".join(out)
    # Real content survived.
    contents = [
        c["delta"]["content"]
        for o in objs
        for c in o.get("choices", [])
        if "delta" in c
    ]
    assert "Hi" in contents and "!" in contents


@pytest.mark.asyncio
async def test_openrouter_comment_glued_to_data_chunk() -> None:
    """Keepalive packed into the same TCP chunk as data (the harder case)."""
    chunks = [
        b'data: {"id":"x","choices":[{"delta":{"content":"a"}}]}\n\n'
        b": OPENROUTER PROCESSING\n\n"
        b'data: {"id":"x","choices":[{"delta":{"content":"b"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = await _drive(chunks)
    objs = _assert_clean(out)
    contents = [
        c["delta"]["content"]
        for o in objs
        for c in o.get("choices", [])
        if "delta" in c
    ]
    assert contents == ["a", "b"]


@pytest.mark.asyncio
async def test_json_split_across_chunk_boundary() -> None:
    """A single event's JSON arriving in two TCP reads must reassemble."""
    chunks = [
        b'data: {"id":"x","choices":[{"delta":{"con',
        b'tent":"split"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = await _drive(chunks)
    objs = _assert_clean(out)
    contents = [
        c["delta"]["content"]
        for o in objs
        for c in o.get("choices", [])
        if "delta" in c
    ]
    assert contents == ["split"]


@pytest.mark.asyncio
async def test_byte_by_byte_fragmentation() -> None:
    """Pathological framing: one byte per chunk. Must still parse cleanly."""
    raw = (
        b'data: {"id":"x","choices":[{"delta":{"content":"drip"}}]}\n\n'
        b": OPENROUTER PROCESSING\n\n"
        b"data: [DONE]\n\n"
    )
    chunks = [raw[i : i + 1] for i in range(len(raw))]
    out = await _drive(chunks)
    objs = _assert_clean(out)
    assert objs and objs[0]["choices"][0]["delta"]["content"] == "drip"


@pytest.mark.asyncio
async def test_gemini_crlf_framing() -> None:
    """Gemini native (alt=sse) frames events with CRLF."""
    chunks = [
        b'data: {"id":"g","choices":[{"delta":{"content":"hej"}}]}\r\n\r\n',
        b'data: {"id":"g","choices":[{"delta":{"content":"!"}}]}\r\n\r\n',
    ]
    out = await _drive(chunks)
    objs = _assert_clean(out)
    contents = [
        c["delta"]["content"]
        for o in objs
        for c in o.get("choices", [])
        if "delta" in c
    ]
    assert contents == ["hej", "!"]


@pytest.mark.asyncio
async def test_azure_leading_role_chunk() -> None:
    """Azure OpenAI opens with a content-filter / role-only chunk."""
    chunks = [
        b'data: {"id":"az","choices":[],"prompt_filter_results":[]}\n\n',
        b'data: {"id":"az","choices":[{"delta":{"role":"assistant"}}]}\n\n',
        b'data: {"id":"az","choices":[{"delta":{"content":"ok"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = await _drive(chunks)
    _assert_clean(out)


@pytest.mark.asyncio
async def test_openrouter_mid_stream_error_event() -> None:
    """OpenRouter mid-stream errors arrive as a normal data JSON event."""
    err = {
        "id": "x",
        "object": "chat.completion.chunk",
        "model": "openai/gpt-4o",
        "error": {"code": "server_error", "message": "Provider disconnected"},
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
    }
    chunks = [
        b'data: {"id":"x","choices":[{"delta":{"content":"partial"}}]}\n\n',
        b"data: " + json.dumps(err).encode() + b"\n\n",
    ]
    out = await _drive(chunks)
    objs = _assert_clean(out)
    assert any("error" in o for o in objs), "error event must be forwarded intact"


@pytest.mark.asyncio
async def test_gemini_combined_content_and_usage_chunk() -> None:
    """Gemini thinking models pack usage into the final *content* chunk.

    Regression: the parser swallowed any chunk carrying a ``usage`` dict, so
    when content + usage arrived together the assistant text was dropped and
    the client saw "no assistant messages" despite a 200 + token accounting.
    """
    chunks = [
        b'data: {"id":"g","choices":[{"delta":{"content":"the answer"},'
        b'"finish_reason":"stop"}],"usage":{"prompt_tokens":3,'
        b'"completion_tokens":2,"total_tokens":5}}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = await _drive(chunks)
    objs = _assert_clean(out)
    contents = [
        c["delta"]["content"]
        for o in objs
        for c in o.get("choices", [])
        if "delta" in c
    ]
    # Content delivered exactly once (not dropped, not duplicated by the trailer).
    assert contents == ["the answer"]


@pytest.mark.asyncio
async def test_separate_usage_chunk_not_forwarded_as_content() -> None:
    """A pure usage chunk (choices: []) is still swallowed, content intact."""
    chunks = [
        b'data: {"id":"x","choices":[{"delta":{"content":"hello"}}]}\n\n',
        b'data: {"id":"x","choices":[],"usage":{"total_tokens":4}}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = await _drive(chunks)
    objs = _assert_clean(out)
    contents = [
        c["delta"]["content"]
        for o in objs
        for c in o.get("choices", [])
        if "delta" in c
    ]
    assert contents == ["hello"]


@pytest.mark.asyncio
async def test_requested_model_override_applied() -> None:
    """Model rewriting still works through the buffered parser."""
    chunks = [
        b'data: {"id":"x","model":"upstream-model","choices":[{"delta":{"content":"hi"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    out = await _drive(chunks, requested_model="routstr-model")
    objs = _assert_clean(out)
    # The upstream content chunk carried model "upstream-model"; the parser must
    # rewrite it to the requested model. (The trailing routstr-generated usage
    # chunk is excluded - it is not an upstream-forwarded chunk.)
    content_chunks = [o for o in objs if o.get("choices")]
    assert content_chunks, "expected at least one forwarded content chunk"
    assert all(o.get("model") == "routstr-model" for o in content_chunks)


@pytest.mark.asyncio
async def test_multiline_non_json_data_each_line_prefixed() -> None:
    """A multi-line non-JSON ``data`` block must keep a ``data:`` prefix per line.

    Two ``data:`` lines in one event reassemble to ``line one\\nline two``, which
    is not JSON, so it takes the raw-forward path. The parser must re-prefix each
    line; a bare second line would reach the client without its ``data:`` field
    and break naive SSE parsers.
    """
    chunks = [
        b"data: line one\ndata: line two\n\n",
        b"data: [DONE]\n\n",
    ]
    out = await _drive(chunks)
    blob = b"".join(out)
    for line in blob.split(b"\n"):
        stripped = line.strip()
        if not stripped or stripped == b"[DONE]":
            continue
        assert line.startswith(b"data: "), f"bare line leaked to client: {line!r}"
    assert b"data: line one" in blob and b"data: line two" in blob
