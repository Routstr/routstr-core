"""Tests for stage timings, the duration header and skipped-path error logging."""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from routstr.core.middleware import LoggingMiddleware, mark
from routstr.core.settings import settings


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def completions(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.getMessage() == "Request completed"]


@pytest.fixture
def records() -> Iterator[_RecordingHandler]:
    handler = _RecordingHandler()
    middleware_logger = logging.getLogger("routstr.core.middleware")
    middleware_logger.setLevel(logging.INFO)
    original_propagate = middleware_logger.propagate
    original_handlers = middleware_logger.handlers
    middleware_logger.propagate = False
    middleware_logger.handlers = [handler]
    try:
        yield handler
    finally:
        middleware_logger.handlers = original_handlers
        middleware_logger.propagate = original_propagate


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    # /v1/wallet/info is in _SKIP_LOG_EXACT, so it exercises the suppression path.
    @app.get("/v1/wallet/info")
    async def wallet_info(fail: bool = False) -> dict[str, str]:
        if fail:
            raise HTTPException(status_code=400, detail="spent token")
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict[str, str]:
        mark(request, "body_read")
        mark(request, "auth")
        return {"status": "ok"}

    @app.post("/v1/chat/completions/stream")
    async def streamed(request: Request) -> StreamingResponse:
        mark(request, "body_read")

        async def body() -> AsyncIterator[bytes]:
            yield b"data: one\n\n"
            await asyncio.sleep(0.05)
            yield b"data: [DONE]\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.get("/admin/api/boom")
    async def boom() -> dict[str, str]:
        raise HTTPException(status_code=500, detail="boom")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_skipped_path_logs_4xx(client: TestClient, records: _RecordingHandler) -> None:
    assert client.get("/v1/wallet/info", params={"fail": True}).status_code == 400

    completions = records.completions()
    assert len(completions) == 1
    record = completions[0]
    assert record.status_code == 400  # type: ignore[attr-defined]
    assert record.path == "/v1/wallet/info"  # type: ignore[attr-defined]
    assert record.method == "GET"  # type: ignore[attr-defined]
    assert record.duration_ms >= 0  # type: ignore[attr-defined]


def test_skipped_path_does_not_log_2xx(
    client: TestClient, records: _RecordingHandler
) -> None:
    assert client.get("/v1/wallet/info").status_code == 200

    assert records.completions() == []


def test_duration_header_present(client: TestClient) -> None:
    response = client.get("/v1/wallet/info")

    assert float(response.headers["x-routstr-duration-ms"]) >= 0


def test_stage_fields_on_completion_log(
    client: TestClient, records: _RecordingHandler
) -> None:
    response = client.post("/v1/chat/completions", json={"model": "m"})
    assert response.status_code == 200

    completions = records.completions()
    assert len(completions) == 1
    record = completions[0]
    assert record.body_read_ms >= 0  # type: ignore[attr-defined]
    assert record.auth_ms >= record.body_read_ms  # type: ignore[attr-defined]
    assert record.content_length == int(  # type: ignore[attr-defined]
        response.request.headers["content-length"]
    )


def test_bogus_content_length_is_dropped(
    client: TestClient, records: _RecordingHandler
) -> None:
    assert (
        client.get(
            "/v1/wallet/info",
            params={"fail": True},
            headers={"content-length": "not-a-number"},
        ).status_code
        == 400
    )

    assert records.completions()[0].content_length is None  # type: ignore[attr-defined]


def test_streamed_duration_covers_the_body(
    client: TestClient, records: _RecordingHandler
) -> None:
    response = client.post("/v1/chat/completions/stream", json={"model": "m"})
    assert response.status_code == 200
    assert response.text.endswith("data: [DONE]\n\n")

    completions = records.completions()
    assert len(completions) == 1
    record = completions[0]
    # The body sleeps 50ms, so a duration that stopped at the headers would be
    # well under it.
    assert record.duration_ms >= 50  # type: ignore[attr-defined]
    assert record.time_to_headers_ms < record.duration_ms  # type: ignore[attr-defined]
    assert record.request_id == response.headers["x-routstr-request-id"]
    assert record.body_read_ms >= 0  # type: ignore[attr-defined]


def test_slow_streamed_request_logs_warning(
    client: TestClient, records: _RecordingHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "slow_request_warn_seconds", 0.02)

    assert (
        client.post("/v1/chat/completions/stream", json={"model": "m"}).status_code
        == 200
    )

    assert records.completions()[0].levelno == logging.WARNING


def test_prefix_skipped_path_still_hides_client_errors(
    client: TestClient, records: _RecordingHandler
) -> None:
    # /admin/api/* is polled on a timer, so an expired session must not turn
    # into one log line per poll; a 500 on the same prefix must still be logged.
    assert client.get("/admin/api/balances").status_code == 404
    assert records.completions() == []

    assert client.get("/admin/api/boom").status_code == 500
    assert len(records.completions()) == 1


def test_slow_request_logs_warning(
    client: TestClient, records: _RecordingHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "slow_request_warn_seconds", 0.0)

    assert client.post("/v1/chat/completions", json={"model": "m"}).status_code == 200

    completions = records.completions()
    assert len(completions) == 1
    assert completions[0].levelno == logging.WARNING
