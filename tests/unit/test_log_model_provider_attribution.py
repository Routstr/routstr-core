"""Model and provider attribution on the request completion/failure log lines."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pythonjsonlogger import jsonlogger

from routstr.core.logging import (
    DailyRotatingFileHandler,
    RequestIdFilter,
    SecurityFilter,
    VersionFilter,
)
from routstr.core.middleware import LoggingMiddleware


@pytest.fixture
def handler(tmp_path: Path) -> Iterator[DailyRotatingFileHandler]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    h = DailyRotatingFileHandler(
        str(log_dir / "app.log"), when="midnight", interval=1, backupCount=30
    )
    h.setLevel(logging.DEBUG)
    h.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s %(pathname)s "
            "%(lineno)d %(version)s %(request_id)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    for f in (VersionFilter(), RequestIdFilter(), SecurityFilter()):
        h.addFilter(f)
    try:
        yield h
    finally:
        h.close()


def _records(handler: DailyRotatingFileHandler) -> list[dict[str, Any]]:
    handler.flush()
    text = Path(handler.baseFilename).read_text()
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


def _record(handler: DailyRotatingFileHandler, message: str) -> dict[str, Any]:
    matches = [r for r in _records(handler) if r.get("message") == message]
    assert matches, f"no {message!r} record was written"
    return matches[-1]


def _build_app() -> FastAPI:
    """Middleware wired like ``main.py``, with routes that set attribution."""
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict:
        request.state.model = "z-ai/glm-5.3-flash"
        request.state.provider = "openrouter"
        return {"ok": True}

    @app.post("/v1/models")
    async def models() -> dict:
        return {"ok": True}

    @app.post("/v1/broken")
    async def broken(request: Request) -> dict:
        request.state.model = "deepseek/deepseek-v4.1-flash"
        request.state.provider = "venice"
        raise RuntimeError("upstream exploded")

    return app


def _with_handler(handler: DailyRotatingFileHandler) -> Any:
    middleware_logger = logging.getLogger("routstr.core.middleware")
    middleware_logger.setLevel(logging.INFO)
    middleware_logger.propagate = False
    original_handlers = middleware_logger.handlers
    middleware_logger.handlers = [handler]

    class _Ctx:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> None:
            middleware_logger.handlers = original_handlers

    return _Ctx()


def test_completion_log_carries_model_and_provider(
    handler: DailyRotatingFileHandler,
) -> None:
    app = _build_app()
    with _with_handler(handler):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/v1/chat/completions", json={"model": "glm-5.3-flash"}
            )
        assert response.status_code == 200

    rec = _record(handler, "Request completed")
    assert rec["model"] == "z-ai/glm-5.3-flash"
    assert rec["provider"] == "openrouter"
    assert rec["status_code"] == 200
    assert isinstance(rec["duration_ms"], (int, float))


def test_completion_log_omits_attribution_when_route_sets_none(
    handler: DailyRotatingFileHandler,
) -> None:
    app = _build_app()
    with _with_handler(handler):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/v1/models", json={})
        assert response.status_code == 200

    rec = _record(handler, "Request completed")
    assert "model" not in rec
    assert "provider" not in rec


def test_failed_request_log_carries_attribution(
    handler: DailyRotatingFileHandler,
) -> None:
    app = _build_app()
    with _with_handler(handler):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/v1/broken", json={})
        assert response.status_code == 500

    rec = _record(handler, "Request failed")
    assert rec["model"] == "deepseek/deepseek-v4.1-flash"
    assert rec["provider"] == "venice"
    assert rec["error_type"] == "RuntimeError"
