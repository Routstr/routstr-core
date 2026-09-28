"""Model and provider attribution on the request completion/failure log lines."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pythonjsonlogger import jsonlogger

from routstr import proxy as proxy_module
from routstr.core.db import get_session
from routstr.core.exceptions import UpstreamError
from routstr.core.logging import (
    DailyRotatingFileHandler,
    RequestIdFilter,
    SecurityFilter,
    VersionFilter,
)
from routstr.core.middleware import LoggingMiddleware, _attribution


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


@contextmanager
def _middleware_logs_to(handler: DailyRotatingFileHandler) -> Iterator[None]:
    middleware_logger = logging.getLogger("routstr.core.middleware")
    saved = (
        middleware_logger.handlers,
        middleware_logger.level,
        middleware_logger.propagate,
    )
    middleware_logger.handlers = [handler]
    middleware_logger.setLevel(logging.INFO)
    middleware_logger.propagate = False
    try:
        yield
    finally:
        (
            middleware_logger.handlers,
            middleware_logger.level,
            middleware_logger.propagate,
        ) = saved


def _record(handler: DailyRotatingFileHandler, message: str) -> dict[str, Any]:
    handler.flush()
    lines = Path(handler.baseFilename).read_text().strip().splitlines()
    matches = [r for r in map(json.loads, lines) if r.get("message") == message]
    assert matches, f"no {message!r} record was written"
    return matches[-1]


# --------------------------------------------------------------------------- #
# Middleware: fields land on the log lines.
# --------------------------------------------------------------------------- #


def _middleware_app() -> FastAPI:
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


def test_completion_log_carries_model_and_provider(
    handler: DailyRotatingFileHandler,
) -> None:
    with _middleware_logs_to(handler):
        with TestClient(_middleware_app(), raise_server_exceptions=False) as client:
            response = client.post(
                "/v1/chat/completions", json={"model": "glm-5.3-flash"}
            )
    assert response.status_code == 200

    rec = _record(handler, "Request completed")
    assert rec["model"] == "z-ai/glm-5.3-flash"
    assert rec["provider"] == "openrouter"
    assert rec["status_code"] == 200


def test_completion_log_omits_attribution_when_route_sets_none(
    handler: DailyRotatingFileHandler,
) -> None:
    with _middleware_logs_to(handler):
        with TestClient(_middleware_app(), raise_server_exceptions=False) as client:
            response = client.post("/v1/models", json={})
    assert response.status_code == 200

    rec = _record(handler, "Request completed")
    assert "model" not in rec
    assert "provider" not in rec


def test_failed_request_log_carries_attribution(
    handler: DailyRotatingFileHandler,
) -> None:
    with _middleware_logs_to(handler):
        with TestClient(_middleware_app(), raise_server_exceptions=False) as client:
            response = client.post("/v1/broken", json={})
    assert response.status_code == 500

    rec = _record(handler, "Request failed")
    assert rec["model"] == "deepseek/deepseek-v4.1-flash"
    assert rec["provider"] == "venice"
    assert rec["error_type"] == "RuntimeError"


def test_middleware_logger_state_is_restored(
    handler: DailyRotatingFileHandler,
) -> None:
    middleware_logger = logging.getLogger("routstr.core.middleware")
    before = (
        list(middleware_logger.handlers),
        middleware_logger.level,
        middleware_logger.propagate,
    )
    with _middleware_logs_to(handler):
        pass
    after = (
        list(middleware_logger.handlers),
        middleware_logger.level,
        middleware_logger.propagate,
    )
    assert after == before


# --------------------------------------------------------------------------- #
# Proxy: which model/provider each routing path attributes the request to.
# --------------------------------------------------------------------------- #


def _model(model_id: str) -> MagicMock:
    return MagicMock(id=model_id)


def _upstream(provider_type: str) -> MagicMock:
    upstream = MagicMock()
    upstream.provider_type = provider_type
    upstream.prepare_headers = MagicMock(return_value={})
    upstream.on_upstream_error_redirect = AsyncMock()
    return upstream


@pytest.fixture
def captured() -> dict[str, object]:
    return {}


@pytest.fixture
def proxy_app(captured: dict[str, object]) -> FastAPI:
    app = FastAPI()
    app.include_router(proxy_module.proxy_router)
    app.dependency_overrides[get_session] = lambda: AsyncMock()

    @app.middleware("http")
    async def capture(request: Request, call_next: Any) -> Response:
        try:
            return await call_next(request)
        finally:
            captured.update(_attribution(request))

    return app


@pytest.fixture
def routing(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub pricing/reservation so only candidate routing drives the test."""
    max_costs: dict[str, int] = {}

    async def max_cost(
        model: str, session: object, model_obj: MagicMock | None = None
    ) -> int:
        return max_costs.get(getattr(model_obj, "id", ""), 100)

    async def discounted(cost: int, body: object, model_obj: object = None) -> int:
        return cost

    state: dict[str, Any] = {
        "candidates": [],
        "max_costs": max_costs,
        "pay": AsyncMock(return_value=MagicMock()),
    }
    monkeypatch.setattr(
        proxy_module, "get_candidates", lambda _model_id: state["candidates"]
    )
    monkeypatch.setattr(proxy_module, "get_max_cost_for_model", max_cost)
    monkeypatch.setattr(proxy_module, "calculate_discounted_max_cost", discounted)
    monkeypatch.setattr(proxy_module, "check_token_balance", lambda *_a: None)
    monkeypatch.setattr(
        proxy_module,
        "get_bearer_token_key",
        AsyncMock(return_value=MagicMock(hashed_key="abcdef123456", balance=0)),
    )
    monkeypatch.setattr(proxy_module, "pay_for_request", state["pay"])
    monkeypatch.setattr(proxy_module, "revert_pay_for_request", AsyncMock())
    monkeypatch.setattr(proxy_module, "_finish_read_transaction", AsyncMock())
    return state


async def _send(app: FastAPI, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_unauthenticated_request_is_attributed_to_the_requested_model(
    proxy_app: FastAPI, routing: dict[str, Any], captured: dict[str, object]
) -> None:
    routing["candidates"] = [(_model("prov/model-a"), _upstream("prov"))]

    response = await _send(
        proxy_app, "POST", "/v1/chat/completions", json={"model": "model-a"}
    )

    assert response.status_code == 401
    assert captured == {"model": "model-a"}


@pytest.mark.parametrize("body", [{}, {"model": "unknown"}, {"model": 123}])
@pytest.mark.asyncio
async def test_request_without_a_model_is_not_attributed(
    proxy_app: FastAPI,
    routing: dict[str, Any],
    captured: dict[str, object],
    body: dict[str, object],
) -> None:
    response = await _send(proxy_app, "POST", "/v1/chat/completions", json=body)

    assert response.status_code == 400
    assert captured == {}


@pytest.mark.asyncio
async def test_paid_fallback_is_attributed_to_the_serving_candidate(
    proxy_app: FastAPI, routing: dict[str, Any], captured: dict[str, object]
) -> None:
    primary, fallback = _upstream("prov-a"), _upstream("prov-b")
    primary.forward_request = AsyncMock(
        side_effect=UpstreamError("down", status_code=502)
    )
    fallback.forward_request = AsyncMock(return_value=Response(status_code=200))
    routing["candidates"] = [
        (_model("prov-a/model-a"), primary),
        (_model("prov-b/model-a-v2"), fallback),
    ]

    response = await _send(
        proxy_app,
        "POST",
        "/v1/chat/completions",
        json={"model": "model-a"},
        headers={"authorization": "Bearer sk-test"},
    )

    assert response.status_code == 200
    assert captured == {"model": "prov-b/model-a-v2", "provider": "prov-b"}


@pytest.mark.asyncio
async def test_fallback_rejected_at_reservation_keeps_last_attempted_attribution(
    proxy_app: FastAPI, routing: dict[str, Any], captured: dict[str, object]
) -> None:
    """A pricier fallback the key cannot reserve is never tried, so the line
    stays with the upstream that actually handled (and failed) the request."""
    primary, fallback = _upstream("prov-a"), _upstream("prov-b")
    primary.forward_request = AsyncMock(
        side_effect=UpstreamError("down", status_code=502)
    )
    fallback.forward_request = AsyncMock()
    routing["candidates"] = [
        (_model("prov-a/model-a"), primary),
        (_model("prov-b/model-a"), fallback),
    ]
    routing["max_costs"]["prov-b/model-a"] = 200
    routing["pay"].side_effect = [
        MagicMock(),
        HTTPException(status_code=402, detail="Insufficient balance"),
    ]

    response = await _send(
        proxy_app,
        "POST",
        "/v1/chat/completions",
        json={"model": "model-a"},
        headers={"authorization": "Bearer sk-test"},
    )

    assert response.status_code == 402
    fallback.forward_request.assert_not_awaited()
    assert captured == {"model": "prov-a/model-a", "provider": "prov-a"}


@pytest.mark.asyncio
async def test_x_cashu_fallback_is_attributed_to_the_serving_candidate(
    proxy_app: FastAPI, routing: dict[str, Any], captured: dict[str, object]
) -> None:
    primary, fallback = _upstream("prov-a"), _upstream("prov-b")
    primary.handle_x_cashu = AsyncMock(
        side_effect=UpstreamError("down", status_code=502)
    )
    fallback.handle_x_cashu = AsyncMock(return_value=Response(status_code=200))
    routing["candidates"] = [
        (_model("prov-a/model-a"), primary),
        (_model("prov-b/model-a"), fallback),
    ]

    response = await _send(
        proxy_app,
        "POST",
        "/v1/chat/completions",
        json={"model": "model-a"},
        headers={"x-cashu": "cashuAtoken"},
    )

    assert response.status_code == 200
    assert captured == {"model": "prov-b/model-a", "provider": "prov-b"}


@pytest.mark.asyncio
async def test_unauthenticated_get_fallback_is_attributed_to_the_serving_upstream(
    proxy_app: FastAPI, routing: dict[str, Any], captured: dict[str, object]
) -> None:
    primary, fallback = _upstream("prov-a"), _upstream("prov-b")
    primary.forward_get_request = AsyncMock(return_value=Response(status_code=502))
    fallback.forward_get_request = AsyncMock(return_value=Response(status_code=200))
    routing["candidates"] = [
        (_model("prov-a/model-a"), primary),
        (_model("prov-b/model-a"), fallback),
    ]

    response = await _send(proxy_app, "GET", "/v1/models")

    assert response.status_code == 200
    assert captured["provider"] == "prov-b"
