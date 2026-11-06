import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import routstr.auth as auth_module
from routstr.auth import ReservationSnapshot, get_reservation_snapshot, pay_for_request
from routstr.core.db import ApiKey, ReservationRelease
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.base import BaseUpstreamProvider

BALANCE = 100_000
RESERVED = 5_000
# 1 msat per prompt token, 2 msat per completion token.
MODEL = Model(
    id="glm-test",
    name="glm-test",
    created=0,
    description="",
    context_length=64_000,
    architecture=Architecture(
        modality="text->text",
        input_modalities=["text"],
        output_modalities=["text"],
        tokenizer="Other",
        instruct_type=None,
    ),
    pricing=Pricing(prompt=0.001, completion=0.002),
    sats_pricing=Pricing(prompt=0.001, completion=0.002),
)
USAGE = {"prompt_tokens": 400, "completion_tokens": 100, "total_tokens": 500}
EXPECTED_MSATS = 400 * 1 + 100 * 2

COMPLETION_BODY = {
    "model": MODEL.id,
    "prompt": "Once upon a time, in a land far away, " * 20,
    "max_tokens": 100,
}
CHAT_BODY = {"model": MODEL.id, "messages": [{"role": "user", "content": "hi"}]}

COMPLETION_JSON = {
    "id": "cmpl-1",
    "object": "text_completion",
    "model": MODEL.id,
    "choices": [{"text": " there was", "index": 0, "finish_reason": "stop"}],
    "usage": USAGE,
}
COMPLETION_CHUNKS = [
    {
        "id": "cmpl-1",
        "object": "text_completion",
        "model": MODEL.id,
        "choices": [{"text": " there", "index": 0, "finish_reason": None}],
    },
    {
        "id": "cmpl-1",
        "object": "text_completion",
        "model": MODEL.id,
        "choices": [{"text": " was", "index": 0, "finish_reason": "stop"}],
    },
]
USAGE_CHUNK = {
    "id": "cmpl-1",
    "object": "text_completion",
    "model": MODEL.id,
    "choices": [],
    "usage": USAGE,
}


def _sse(chunks: list[dict]) -> bytes:
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + b"data: [DONE]\n\n"


@pytest.fixture(autouse=True)
def patch_sats_usd_price() -> Any:
    with patch("routstr.payment.cost_calculation.sats_usd_price", return_value=5.0e-4):
        yield


async def _engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    return engine


def _upstream(content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("POST", "http://upstream"),
    )


async def _drain(response: Any) -> bytes:
    body = b""
    if hasattr(response, "body_iterator"):
        async for chunk in response.body_iterator:
            body += chunk if isinstance(chunk, bytes) else chunk.encode()
    else:
        body = response.body
    return body


async def _forward(
    engine: AsyncEngine,
    path: str,
    body: dict,
    upstream: httpx.Response,
    native_messages: bool = False,
) -> tuple[bytes, ReservationSnapshot, AsyncMock]:
    """Reserve, forward through the real ``forward_request`` and settle."""
    provider = BaseUpstreamProvider(
        base_url="http://upstream", api_key="k", provider_fee=1.0
    )
    provider.supports_anthropic_messages = native_messages
    request = MagicMock()
    request.method = "POST"
    request.query_params = {}
    send = AsyncMock(return_value=upstream)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = ApiKey(hashed_key="key", balance=BALANCE)
        session.add(key)
        await session.commit()
        await pay_for_request(key, RESERVED, session)
        snapshot = await get_reservation_snapshot(key, session)

        with (
            patch("httpx.AsyncClient.send", send),
            patch(
                "routstr.upstream.base.create_session",
                side_effect=lambda: AsyncSession(engine, expire_on_commit=False),
            ),
            patch(
                "routstr.upstream.base.adjust_payment_for_tokens",
                auth_module.adjust_payment_for_tokens,
            ),
        ):
            forward = (
                provider.forward_responses_request
                if path.endswith("responses")
                else provider.forward_request
            )
            response = await forward(
                request,
                path,
                {},
                json.dumps(body).encode(),
                key,
                RESERVED,
                session,
                MODEL,
                snapshot,
            )
            out = await _drain(response)
    return out, snapshot, send


async def _ledger(
    engine: AsyncEngine, snapshot: ReservationSnapshot
) -> tuple[int, int, int, str | None]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        key = await session.get(ApiKey, snapshot.key_hash)
        record = await session.get(ReservationRelease, snapshot.release_id)
        assert key is not None
        return (
            key.balance,
            key.total_spent,
            key.reserved_balance,
            record.status if record else None,
        )


def _sse_objects(out: bytes) -> list[dict]:
    objs = []
    for line in out.split(b"\n"):
        if line.startswith(b"data: ") and line[6:].strip() != b"[DONE]":
            objs.append(json.loads(line[6:]))
    return objs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "completions",
        "v1/completions",
        "v1/completions/",
        "openai/v1/completions",
    ],
)
async def test_non_streaming_completion_with_usage_is_charged(path: str) -> None:
    engine = await _engine()
    out, snapshot, _ = await _forward(
        engine,
        path,
        COMPLETION_BODY,
        _upstream(json.dumps(COMPLETION_JSON).encode(), "application/json"),
    )

    balance, spent, reserved, status = await _ledger(engine, snapshot)
    assert (balance, spent, reserved, status) == (
        BALANCE - EXPECTED_MSATS,
        EXPECTED_MSATS,
        0,
        "charged",
    )
    body = json.loads(out)
    assert body["object"] == "text_completion"
    assert body["usage"]["cost"]["total_msats"] == EXPECTED_MSATS
    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_completion_with_final_usage_is_charged() -> None:
    engine = await _engine()
    out, snapshot, send = await _forward(
        engine,
        "v1/completions",
        {**COMPLETION_BODY, "stream": True},
        _upstream(_sse([*COMPLETION_CHUNKS, USAGE_CHUNK]), "text/event-stream"),
    )

    balance, spent, reserved, status = await _ledger(engine, snapshot)
    assert (balance, spent, reserved, status) == (
        BALANCE - EXPECTED_MSATS,
        EXPECTED_MSATS,
        0,
        "charged",
    )

    forwarded = json.loads(send.call_args.args[0].content)
    assert forwarded["stream_options"] == {"include_usage": True}

    objs = _sse_objects(out)
    assert [o["choices"][0]["text"] for o in objs if o["choices"]] == [
        " there",
        " was",
    ]
    assert objs[-1]["object"] == "text_completion"
    assert objs[-1]["usage"]["cost"]["total_msats"] == EXPECTED_MSATS
    assert out.endswith(b"data: [DONE]\n\n")
    await engine.dispose()


@pytest.mark.asyncio
async def test_non_streaming_completion_with_empty_usage_is_estimated() -> None:
    """Missing usage is estimated from ``prompt`` and ``text``, never free."""
    engine = await _engine()
    no_usage = {k: v for k, v in COMPLETION_JSON.items() if k != "id"}
    no_usage["usage"] = {}
    out, snapshot, _ = await _forward(
        engine,
        "v1/completions",
        COMPLETION_BODY,
        _upstream(json.dumps(no_usage).encode(), "application/json"),
    )

    balance, spent, reserved, status = await _ledger(engine, snapshot)
    assert 0 < spent <= RESERVED
    assert (BALANCE - balance, reserved, status) == (spent, 0, "charged")
    body = json.loads(out)
    usage = body["usage"]
    assert body["id"].startswith("cmpl-")
    assert usage["estimated"] is True
    assert usage["prompt_tokens"] > 100
    assert usage["completion_tokens"] > 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_completion_without_usage_is_estimated() -> None:
    engine = await _engine()
    out, snapshot, _ = await _forward(
        engine,
        "v1/completions",
        {**COMPLETION_BODY, "stream": True},
        _upstream(_sse(COMPLETION_CHUNKS), "text/event-stream"),
    )

    balance, spent, reserved, status = await _ledger(engine, snapshot)
    assert 0 < spent <= RESERVED
    assert (BALANCE - balance, reserved, status) == (spent, 0, "charged")
    trailer = _sse_objects(out)[-1]
    assert trailer["id"] == "cmpl-1"
    assert trailer["object"] == "text_completion"
    assert trailer["usage"]["prompt_tokens"] > 100
    assert trailer["usage"]["cost"]["total_msats"] == spent
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1/chat/completions/", "openai/v1/chat/completions"])
async def test_chat_completion_aliases_are_charged(path: str) -> None:
    engine = await _engine()
    chat_json = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": MODEL.id,
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "index": 0,
                "finish_reason": "stop",
            }
        ],
        "usage": USAGE,
    }
    out, snapshot, _ = await _forward(
        engine,
        path,
        CHAT_BODY,
        _upstream(json.dumps(chat_json).encode(), "application/json"),
    )

    balance, spent, reserved, status = await _ledger(engine, snapshot)
    assert (balance, spent, reserved, status) == (
        BALANCE - EXPECTED_MSATS,
        EXPECTED_MSATS,
        0,
        "charged",
    )
    assert json.loads(out)["usage"]["cost"]["total_msats"] == EXPECTED_MSATS
    await engine.dispose()


ERROR_BODY = {"error": {"message": "Provider returned error"}}
FAILED_RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "status": "failed",
    "error": {"code": "server_error", "message": "Provider returned error"},
    "output": [],
}
ANTHROPIC_ERROR = {
    "type": "error",
    "error": {"type": "overloaded_error", "message": "Overloaded"},
}
MESSAGES_BODY = {
    "model": MODEL.id,
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "hi"}],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "stream", "content", "content_type"),
    [
        (
            "v1/chat/completions",
            CHAT_BODY,
            False,
            json.dumps(ERROR_BODY).encode(),
            "application/json",
        ),
        (
            "v1/chat/completions",
            CHAT_BODY,
            True,
            _sse(
                [
                    {
                        "id": "gen-1",
                        "object": "chat.completion.chunk",
                        "model": MODEL.id,
                        "error": {"code": 502, "message": "Provider returned error"},
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": ""},
                                "finish_reason": "error",
                            }
                        ],
                    }
                ]
            ),
            "text/event-stream",
        ),
        (
            "v1/chat/completions",
            CHAT_BODY,
            False,
            json.dumps(
                {
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "model": MODEL.id,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": None},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": None,
                }
            ).encode(),
            "application/json",
        ),
        (
            "v1/responses",
            {"model": MODEL.id, "input": "hi"},
            False,
            json.dumps(FAILED_RESPONSE).encode(),
            "application/json",
        ),
        (
            "v1/responses",
            {"model": MODEL.id, "input": "hi"},
            False,
            json.dumps({**FAILED_RESPONSE, "usage": None}).encode(),
            "application/json",
        ),
        (
            "v1/responses",
            {"model": MODEL.id, "input": "hi"},
            True,
            _sse([{"type": "response.failed", "response": FAILED_RESPONSE}]),
            "text/event-stream",
        ),
        (
            "v1/messages",
            MESSAGES_BODY,
            False,
            json.dumps(ANTHROPIC_ERROR).encode(),
            "application/json",
        ),
        (
            "v1/messages",
            MESSAGES_BODY,
            True,
            b"event: error\ndata: " + json.dumps(ANTHROPIC_ERROR).encode() + b"\n\n",
            "text/event-stream",
        ),
    ],
)
async def test_reply_with_no_output_and_no_usage_is_free(
    path: str, body: dict, stream: bool, content: bytes, content_type: str
) -> None:
    """Nothing generated and no usage reported: no charge, as with X-Cashu."""
    engine = await _engine()
    _, snapshot, _ = await _forward(
        engine,
        path,
        {**body, "stream": stream},
        _upstream(content, content_type),
        native_messages=path.endswith("messages"),
    )

    balance, spent, reserved, _ = await _ledger(engine, snapshot)
    assert (balance, spent, reserved) == (BALANCE, 0, 0)
    await engine.dispose()


def test_stream_usage_option_is_scoped_to_completion_endpoints() -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="k")
    body = json.dumps({"prompt": "draw this", "stream": True}).encode()

    assert provider.prepare_request_body(body, MODEL) == body


async def _forward_x_cashu(
    path: str, body: dict, upstream: httpx.Response
) -> tuple[AsyncMock, AsyncMock]:
    """Run ``forward_x_cashu_request`` with the settlement handler stubbed out."""
    provider = BaseUpstreamProvider(
        base_url="http://upstream", api_key="k", provider_fee=1.0
    )
    request = MagicMock()
    request.method = "POST"
    request.query_params = {}
    request.state.request_id = "req-1"
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    send = AsyncMock(return_value=upstream)
    settle = AsyncMock(return_value=Response(content=b"{}", status_code=200))

    with (
        patch("httpx.AsyncClient.send", send),
        patch.object(provider, "handle_x_cashu_chat_completion", settle),
    ):
        await provider.forward_x_cashu_request(
            request, path, {}, 10, "sat", RESERVED, MODEL
        )
    return settle, send


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["completions", "v1/completions", "v1/completions/"])
async def test_x_cashu_legacy_completion_is_settled(path: str) -> None:
    """Legacy completions must reach refund settlement, not raw passthrough."""
    settle, _ = await _forward_x_cashu(
        path,
        COMPLETION_BODY,
        _upstream(json.dumps(COMPLETION_JSON).encode(), "application/json"),
    )

    assert settle.await_count == 1


@pytest.mark.asyncio
async def test_x_cashu_streaming_completion_requests_usage() -> None:
    _, send = await _forward_x_cashu(
        "v1/completions",
        {**COMPLETION_BODY, "stream": True},
        _upstream(_sse([*COMPLETION_CHUNKS, USAGE_CHUNK]), "text/event-stream"),
    )

    forwarded = json.loads(send.call_args.args[0].content)
    assert forwarded["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"x": 7}])
@pytest.mark.parametrize("stream", [False, True])
async def test_native_messages_tool_output_without_usage_is_billed(
    arguments: dict, stream: bool
) -> None:
    tool = {"type": "tool_use", "id": "toolu_1", "name": "calc", "input": arguments}
    content = (
        _sse(
            [
                {"type": "content_block_start", "index": 0, "content_block": tool},
                {"type": "message_stop"},
            ]
        )
        if stream
        else json.dumps(
            {"type": "message", "content": [tool], "stop_reason": "tool_use"}
        ).encode()
    )
    engine = await _engine()
    out, snapshot, _ = await _forward(
        engine,
        "v1/messages",
        {**MESSAGES_BODY, "stream": stream},
        _upstream(content, "text/event-stream" if stream else "application/json"),
        native_messages=True,
    )

    balance, spent, reserved, status = await _ledger(engine, snapshot)
    assert b"toolu_1" in out
    assert spent > 0
    assert (balance, reserved, status) == (BALANCE - spent, 0, "charged")
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", [b"data:", b"data: "])
@pytest.mark.parametrize("multiline", [False, True])
async def test_native_messages_sse_data_fields_are_billed(
    separator: bytes, multiline: bool
) -> None:
    payload = json.dumps(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hello"},
        }
    ).encode()
    if multiline:
        payload = payload.replace(b', "index"', b',\n"index"', 1)
    content = (
        b"event: content_block_delta\nid: delta-1\n"
        + b"".join(separator + line + b"\n" for line in payload.split(b"\n"))
        + b"\n"
    )
    engine = await _engine()
    out, snapshot, _ = await _forward(
        engine,
        "v1/messages",
        {**MESSAGES_BODY, "stream": True},
        _upstream(content, "text/event-stream"),
        native_messages=True,
    )

    balance, spent, reserved, status = await _ledger(engine, snapshot)
    assert b"hello" in out
    assert b"event: content_block_delta\nid: delta-1\ndata:" in out
    assert spent > 0
    assert (balance, reserved, status) == (BALANCE - spent, 0, "charged")
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_tokens", [None, 0, 400])
async def test_responses_failed_preserves_usage_and_single_terminal_event(
    input_tokens: int | None,
) -> None:
    failed = dict(FAILED_RESPONSE)
    if input_tokens is not None:
        failed["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "total_tokens": input_tokens,
        }
    event = {"type": "response.failed", "response": failed}
    content = b"event: response.failed\nid: failed-1\n" + _sse([event])
    engine = await _engine()
    out, snapshot, _ = await _forward(
        engine,
        "v1/responses",
        {"model": MODEL.id, "input": "hi", "stream": True},
        _upstream(content, "text/event-stream"),
    )

    events = [
        json.loads(line[6:])
        for line in out.splitlines()
        if line.startswith(b"data: ") and line != b"data: [DONE]"
    ]
    assert [item["type"] for item in events] == ["response.failed"]
    assert events[0]["response"]["status"] == "failed"
    assert events[0]["response"]["error"] == failed["error"]
    assert b"event: response.failed\nid: failed-1\ndata:" in out
    assert out.count(b"data: [DONE]") == 1
    if input_tokens is not None:
        assert events[0]["response"]["usage"]["input_tokens"] == input_tokens
    balance, spent, reserved, status = await _ledger(engine, snapshot)
    assert (balance, spent, reserved, status) == (
        BALANCE - (input_tokens or 0),
        input_tokens or 0,
        0,
        "charged",
    )
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_event", ["message_start", "message_delta", "both"])
@pytest.mark.parametrize("content_type", ["tool_use", "text"])
async def test_native_messages_stream_explicit_zero_usage_wins_over_estimate(
    usage_event: str, content_type: str
) -> None:
    start: dict = {"type": "message_start", "message": {"content": []}}
    delta: dict = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
    if usage_event in ("message_start", "both"):
        start["message"]["usage"] = {"input_tokens": 0, "output_tokens": 0}
    if usage_event in ("message_delta", "both"):
        delta["usage"] = {"output_tokens": 0}
    block = (
        {"type": "tool_use", "id": "toolu_1", "name": "calc", "input": {}}
        if content_type == "tool_use"
        else {"type": "text", "text": "hello"}
    )
    engine = await _engine()
    out, snapshot, _ = await _forward(
        engine,
        "v1/messages",
        {**MESSAGES_BODY, "stream": True},
        _upstream(
            _sse(
                [
                    start,
                    {"type": "content_block_start", "index": 0, "content_block": block},
                    delta,
                    {"type": "message_stop"},
                ]
            ),
            "text/event-stream",
        ),
        native_messages=True,
    )

    assert (b"toolu_1" if content_type == "tool_use" else b"hello") in out
    assert await _ledger(engine, snapshot) == (BALANCE, 0, 0, "charged")
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", [b"", b"\n", b"\r", b"\r\n"])
async def test_native_messages_rewrite_preserves_unterminated_sse_tail(
    ending: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks: list[bytes] = []

    async def collect_chunks(response: Any) -> bytes:
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    monkeypatch.setattr(f"{__name__}._drain", collect_chunks)
    content = (
        b"data: "
        + json.dumps(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            }
        ).encode()
        + ending
    )
    engine = await _engine()
    _, snapshot, _ = await _forward(
        engine,
        "v1/messages",
        {**MESSAGES_BODY, "stream": True},
        _upstream(content, "text/event-stream"),
        native_messages=True,
    )

    # The guard normalizes CRLF, but does not complete an unterminated event.
    assert chunks[0][len(chunks[0].rstrip(b"\r\n")) :] == ending.replace(b"\r\n", b"\n")
    assert not chunks[0].endswith(b"\n\n")
    assert json.loads(chunks[0][len(b"data: ") :])["delta"]["text"] == "hello"
    assert (await _ledger(engine, snapshot))[2:] == (0, "charged")
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_tokens", [None, 0, 400])
@pytest.mark.parametrize("interruption", ["protocol_error", "client_cancel"])
async def test_responses_failed_settles_and_emits_before_stream_interruption(
    input_tokens: int | None, interruption: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = dict(FAILED_RESPONSE)
    if input_tokens is not None:
        failed["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "total_tokens": input_tokens,
        }
    event = {"type": "response.failed", "response": failed}
    content = (
        b"event: response.failed\nid: failed-1\ndata: "
        + json.dumps(event).encode()
        + b"\n\n"
    )
    resumed = False
    closed = 0

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncGenerator[bytes, None]:
            nonlocal resumed
            yield content
            resumed = True
            raise httpx.RemoteProtocolError("incomplete chunked read")

        async def aclose(self) -> None:
            nonlocal closed
            closed += 1

    if interruption == "client_cancel":

        async def cancel_downstream(response: Any) -> bytes:
            emitted: list[bytes] = []

            async def receive() -> dict[str, str]:
                return {"type": "http.disconnect"}

            async def send(message: dict) -> None:
                if message["type"] == "http.response.body":
                    emitted.append(message["body"])
                    raise asyncio.CancelledError

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "method": "POST",
                "path": "/v1/responses",
                "headers": [],
            }
            with pytest.raises(asyncio.CancelledError):
                await response(scope, receive, send)
            return b"".join(emitted)

        monkeypatch.setattr(f"{__name__}._drain", cancel_downstream)

    engine = await _engine()
    adjust = AsyncMock(wraps=auth_module.adjust_payment_for_tokens)
    with patch.object(auth_module, "adjust_payment_for_tokens", adjust):
        out, snapshot, _ = await _forward(
            engine,
            "v1/responses",
            {"model": MODEL.id, "input": "hi", "stream": True},
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=InterruptedStream(),
                request=httpx.Request("POST", "http://upstream"),
            ),
        )

    events = _sse_objects(out)
    assert [item["type"] for item in events] == ["response.failed"]
    assert events[0]["response"]["status"] == "failed"
    assert events[0]["response"]["error"] == failed["error"]
    assert b"event: response.failed\nid: failed-1\ndata:" in out
    assert b"[DONE]" not in out
    adjust.assert_awaited_once()
    assert resumed is (interruption == "protocol_error")
    assert closed == 1
    assert await _ledger(engine, snapshot) == (
        BALANCE - (input_tokens or 0),
        input_tokens or 0,
        0,
        "charged",
    )
    await engine.dispose()
