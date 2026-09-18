import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.types import Message, Send

from routstr.upstream.base import BaseUpstreamProvider, _OwnedUpstreamStream


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.close_count = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.payload

    async def aclose(self) -> None:
        self.close_count += 1


class _CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload: bytes) -> None:
        self.stream = _CountingStream(payload)
        self.close_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, stream=self.stream)

    async def aclose(self) -> None:
        self.close_count += 1


class _CountingClient(httpx.AsyncClient):
    def __init__(self, transport: _CountingTransport) -> None:
        super().__init__(transport=transport)
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1
        await super().aclose()


def _request() -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "scheme": "http",
            "path": "/v1/audio/speech",
            "raw_path": b"/v1/audio/speech",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
        },
        receive,
    )


async def _forward(
    provider: BaseUpstreamProvider,
    method_name: str,
) -> tuple[StreamingResponse, _CountingClient, _CountingTransport]:
    transport = _CountingTransport(b"live-stream")
    client = _CountingClient(transport)
    model = MagicMock()

    with patch("routstr.upstream.base.httpx.AsyncClient", return_value=client):
        result = await getattr(provider, method_name)(
            request=_request(),
            path="v1/audio/speech",
            headers={},
            amount=10,
            unit="sat",
            max_cost_for_model=10_000,
            model_obj=model,
        )

    assert isinstance(result, StreamingResponse)
    return result, client, transport


async def _run_asgi_response(
    response: StreamingResponse,
    send: Send,
) -> None:
    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    await response(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
        },
        receive,
        send,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["forward_x_cashu_request", "forward_x_cashu_responses_request"],
)
async def test_x_cashu_opaque_stream_owns_client_until_normal_completion(
    method_name: str,
) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    response, client, transport = await _forward(provider, method_name)
    messages: list[dict[str, Any]] = []

    assert client.close_count == 0
    assert transport.stream.close_count == 0

    async def send(message: Message) -> None:
        messages.append(dict(message))

    await _run_asgi_response(response, send)

    assert (
        b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        == b"live-stream"
    )
    assert transport.stream.close_count == 1
    assert client.close_count == 1
    assert transport.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["forward_x_cashu_request", "forward_x_cashu_responses_request"],
)
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("downstream send failed"), asyncio.CancelledError()],
)
async def test_x_cashu_opaque_stream_closes_client_when_send_fails(
    method_name: str,
    failure: BaseException,
) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    response, client, transport = await _forward(provider, method_name)

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            raise failure

    with pytest.raises(type(failure)):
        await _run_asgi_response(response, send)

    assert transport.stream.close_count == 1
    assert client.close_count == 1
    assert transport.close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "path", "payload"),
    [
        (
            "forward_x_cashu_request",
            "v1/chat/completions",
            b'data: {"model":"m","usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n',
        ),
        (
            "forward_x_cashu_responses_request",
            "v1/responses",
            b'data: {"type":"response.completed","response":{"model":"m","usage":{"input_tokens":1,"output_tokens":1}}}\n\ndata: [DONE]\n\n',
        ),
    ],
)
@pytest.mark.parametrize(
    "failure", [None, RuntimeError("send failed"), asyncio.CancelledError()]
)
async def test_x_cashu_real_processed_stream_releases_buffered_upstream_promptly(
    method_name: str,
    path: str,
    payload: bytes,
    failure: BaseException | None,
) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    transport = _CountingTransport(payload)
    client = _CountingClient(transport)

    with (
        patch("routstr.upstream.base.httpx.AsyncClient", return_value=client),
        patch.object(provider, "get_x_cashu_cost", new=AsyncMock(return_value=None)),
    ):
        result = await getattr(provider, method_name)(
            request=_request(),
            path=path,
            headers={},
            amount=10,
            unit="sat",
            max_cost_for_model=10_000,
            model_obj=MagicMock(),
        )

    assert isinstance(result, StreamingResponse)
    assert transport.stream.close_count == 1
    assert client.close_count == 1
    assert transport.close_count == 1

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)
        if failure is not None and message["type"] == "http.response.body":
            if message.get("body"):
                raise failure

    if failure is None:
        await _run_asgi_response(result, send)
        assert any(message.get("body") for message in messages)
    else:
        with pytest.raises(type(failure)):
            await _run_asgi_response(result, send)

    assert transport.stream.close_count == 1
    assert client.close_count == 1
    assert transport.close_count == 1


@pytest.mark.asyncio
async def test_owned_upstream_cleanup_survives_caller_cancellation() -> None:
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    client_close_count = 0

    async def body() -> AsyncIterator[bytes]:
        yield b"body"

    response = MagicMock(spec=httpx.Response)
    response.aclose = AsyncMock()
    client = MagicMock(spec=httpx.AsyncClient)

    async def close_client() -> None:
        nonlocal client_close_count
        client_close_count += 1
        cleanup_started.set()
        await allow_cleanup.wait()
        cleanup_finished.set()

    client.aclose = close_client
    owned = _OwnedUpstreamStream(body(), response, client)

    first_close = asyncio.create_task(owned.aclose())
    await cleanup_started.wait()
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    allow_cleanup.set()
    await asyncio.wait_for(cleanup_finished.wait(), timeout=1)
    await owned.aclose()

    assert response.aclose.await_count == 1
    assert client_close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "path", "handler_name"),
    [
        (
            "forward_x_cashu_request",
            "v1/chat/completions",
            "handle_x_cashu_chat_completion",
        ),
        (
            "forward_x_cashu_responses_request",
            "v1/responses",
            "handle_x_cashu_responses_completion",
        ),
    ],
)
async def test_x_cashu_non_streaming_result_closes_upstream_promptly(
    method_name: str,
    path: str,
    handler_name: str,
) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    transport = _CountingTransport(b"{}")
    client = _CountingClient(transport)

    with (
        patch("routstr.upstream.base.httpx.AsyncClient", return_value=client),
        patch.object(
            provider,
            handler_name,
            new=AsyncMock(return_value=Response(b"done")),
        ),
    ):
        result = await getattr(provider, method_name)(
            request=_request(),
            path=path,
            headers={},
            amount=10,
            unit="sat",
            max_cost_for_model=10_000,
            model_obj=MagicMock(),
        )

    assert not isinstance(result, StreamingResponse)
    assert transport.stream.close_count == 1
    assert client.close_count == 1
    assert transport.close_count == 1
