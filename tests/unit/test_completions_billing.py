import json
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
) -> tuple[bytes, ReservationSnapshot, AsyncMock]:
    """Reserve, forward through the real ``forward_request`` and settle."""
    provider = BaseUpstreamProvider(
        base_url="http://upstream", api_key="k", provider_fee=1.0
    )
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
            response = await provider.forward_request(
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
