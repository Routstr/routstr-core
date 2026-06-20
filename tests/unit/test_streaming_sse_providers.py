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


@pytest.mark.asyncio
async def test_crlf_delimiter_split_across_chunk_boundary() -> None:
    """CRLF event delimiter straddling two TCP reads must not merge events.

    Regression: a per-chunk ``replace(b"\\r\\n", b"\\n")`` left a stray ``\\r``
    when a ``\\r\\n`` of the ``\\r\\n\\r\\n`` delimiter landed at the very end of
    one ``aiter_bytes`` chunk and the matching ``\\n`` opened the next. The
    ``\\n\\n`` split then missed the boundary, glued two events into one frame
    with two ``data:`` lines, and the client's ``JSON.parse`` threw on the
    concatenated payload (the "unexpected token"/"Extra data" crash).
    """
    e1 = b'data: {"id":"x","choices":[{"delta":{"content":"a"}}]}'
    e2 = b'data: {"id":"x","choices":[{"delta":{"content":"b"}}]}'
    chunks = [
        e1 + b"\r\n\r",  # delimiter cut mid-CRLF
        b"\n" + e2 + b"\r\n\r\n",
        b"data: [DONE]\r\n\r\n",
    ]
    out = await _drive(chunks)

    # Client-accurate check: a real SSE client concatenates all ``data:`` lines
    # *within one event* (events are ``\n\n``-delimited) before parsing. A
    # merged frame would surface here as two objects glued into one payload,
    # which ``_assert_clean`` (per-line) would miss.
    blob = b"".join(out)
    contents: list[str] = []
    for event in blob.split(b"\n\n"):
        datas = [
            ln[len(b"data: ") :]
            for ln in event.split(b"\n")
            if ln.startswith(b"data: ")
        ]
        if not datas:
            continue
        payload = b"".join(datas)
        if payload.strip() == b"[DONE]":
            continue
        obj = json.loads(payload)  # raises if two events were merged into one
        for c in obj.get("choices", []):
            if "delta" in c:
                contents.append(c["delta"]["content"])
    assert contents == ["a", "b"]


@pytest.mark.asyncio
async def test_truncated_json_tail_on_connection_close() -> None:
    """A stream that drops mid-event must not emit the partial JSON downstream.

    Regression: the end-of-stream flush ran ``_process_event`` on the leftover
    buffer unconditionally. When the upstream connection closed mid-event the
    leftover was incomplete JSON, which fell through to the raw-forward path and
    handed the client a ``data: {partial`` frame -> ``Unterminated string`` parse
    error. The truncated tail must be dropped instead.
    """
    chunks = [
        b'data: {"id":"x","choices":[{"delta":{"content":"ok"}}]}\n\n',
        b'data: {"id":"x","choices":[{"delta":{"con',  # connection dies here
    ]
    out = await _drive(chunks)
    objs = _assert_clean(out)  # raises if the partial tail leaked as a data frame
    contents = [
        c["delta"]["content"]
        for o in objs
        for c in o.get("choices", [])
        if "delta" in c
    ]
    # The one complete chunk is delivered; the truncated fragment is dropped
    # entirely (no second delta), and _assert_clean above guarantees nothing
    # non-JSON ever reached the client.
    assert contents == ["ok"]
