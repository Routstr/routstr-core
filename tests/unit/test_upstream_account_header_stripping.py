"""Upstream account identifiers must not reach the client."""

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from routstr.core.middleware import LoggingMiddleware

UPSTREAM_HEADERS = {
    "anthropic-workspace-id": "wrkspc_secret",
    "anthropic-organization-id": "00000000-0000-0000-0000-000000000000",
    "request-id": "req_011",
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.post("/v1/messages")
    async def messages() -> Response:
        return JSONResponse({"ok": True}, headers=UPSTREAM_HEADERS)

    @app.post("/v1/messages/stream")
    async def messages_stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"event: message_stop\ndata: {}\n\n"

        return StreamingResponse(
            body(), media_type="text/event-stream", headers=UPSTREAM_HEADERS
        )

    @app.post("/v1/messages/error")
    async def messages_error() -> Response:
        return JSONResponse(
            {"type": "error"},
            status_code=400,
            headers={"Anthropic-Workspace-Id": "wrkspc_secret"},
        )

    yield TestClient(app)


@pytest.mark.parametrize(
    "path", ["/v1/messages", "/v1/messages/stream", "/v1/messages/error"]
)
def test_upstream_account_headers_are_stripped(client: TestClient, path: str) -> None:
    response = client.post(path)

    assert "anthropic-workspace-id" not in response.headers
    assert "anthropic-organization-id" not in response.headers
    assert "wrkspc_" not in str(response.headers)


def test_other_upstream_headers_are_kept(client: TestClient) -> None:
    response = client.post("/v1/messages")

    assert response.headers["request-id"] == "req_011"
    assert "x-routstr-request-id" in response.headers
