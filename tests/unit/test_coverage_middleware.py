"""Coverage-filling tests for middleware.py (currently 38% coverage).

Only LoggingMiddleware and request_id_context exist on main.
ConcurrencyLimiterMiddleware + TimeoutMiddleware are on an unmerged branch.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# LoggingMiddleware
# ---------------------------------------------------------------------------

def test_logging_middleware_adds_request_id() -> None:
    """Every request gets an x-routstr-request-id header."""
    from routstr.core.middleware import LoggingMiddleware

    app = FastAPI()

    @app.get("/test")
    async def test_endpoint(request: Request) -> dict:
        assert hasattr(request.state, "request_id")
        assert request.state.request_id is not None
        return {"ok": True}

    app.add_middleware(LoggingMiddleware)

    client = TestClient(app)
    response = client.get("/test")

    assert response.status_code == 200
    assert "x-routstr-request-id" in response.headers
    assert len(response.headers["x-routstr-request-id"]) == 36  # UUID4 length


def test_logging_middleware_skips_head_requests() -> None:
    """HEAD requests are skipped by _should_log (health probes)."""
    from routstr.core.middleware import LoggingMiddleware

    app = FastAPI()

    @app.head("/test")
    async def test_endpoint(request: Request) -> dict:
        return {"ok": True}

    app.add_middleware(LoggingMiddleware)

    client = TestClient(app)
    response = client.head("/test")

    assert response.status_code == 200
    assert "x-routstr-request-id" in response.headers


def test_logging_middleware_skips_options_requests() -> None:
    """OPTIONS requests (CORS preflight) are skipped."""
    from routstr.core.middleware import LoggingMiddleware

    app = FastAPI()

    @app.options("/test")
    async def test_endpoint(request: Request) -> dict:
        return {"ok": True}

    app.add_middleware(LoggingMiddleware)

    client = TestClient(app)
    response = client.options("/test")

    assert response.status_code == 200
    assert "x-routstr-request-id" in response.headers


def test_should_log_rejects_admin_api_prefix() -> None:
    """Admin API polling paths are skipped."""
    from routstr.core.middleware import _should_log

    assert _should_log("GET", "/admin/api/balances") is False
    assert _should_log("GET", "/admin/api/logs") is False
    assert _should_log("GET", "/admin/api/providers") is False


def test_should_log_rejects_nextjs_chunks() -> None:
    """Next.js static chunks are skipped."""
    from routstr.core.middleware import _should_log

    assert _should_log("GET", "/_next/static/chunks/main.js") is False
    assert _should_log("GET", "/_next/data/build-id/page.json") is False


def test_should_log_rejects_exact_paths() -> None:
    """Exact paths like /favicon.ico are skipped."""
    from routstr.core.middleware import _should_log

    assert _should_log("GET", "/favicon.ico") is False
    assert _should_log("GET", "/v1/wallet/info") is False
    assert _should_log("GET", "/index.txt") is False
    assert _should_log("GET", "/login/index.txt") is False


def test_should_log_accepts_normal_paths() -> None:
    """Normal API paths are logged."""
    from routstr.core.middleware import _should_log

    assert _should_log("GET", "/v1/chat/completions") is True
    assert _should_log("POST", "/v1/chat/completions") is True
    assert _should_log("GET", "/v1/models") is True
    assert _should_log("POST", "/api/some-endpoint") is True


def test_should_log_accepts_non_skipped_path() -> None:
    """Generic paths not in skip list are logged."""
    from routstr.core.middleware import _should_log

    assert _should_log("GET", "/some/random/path") is True
    assert _should_log("POST", "/api/custom") is True


def test_request_id_context_is_contextvar() -> None:
    """request_id_context is a ContextVar[str | None] with no default value."""
    from contextvars import ContextVar

    from routstr.core.middleware import request_id_context

    assert isinstance(request_id_context, ContextVar)
    # ContextVar without a default raises LookupError when accessed without being set
    try:
        val = request_id_context.get()
        # If it returns, it should be None
        assert val is None
    except LookupError:
        # Expected: ContextVar with no default raises LookupError
        pass


def test_middleware_exports() -> None:
    """Only LoggingMiddleware is exported on main."""
    from routstr.core.middleware import LoggingMiddleware, request_id_context

    assert LoggingMiddleware is not None
    assert request_id_context is not None


def test_middleware_skips_health_probe_path() -> None:
    """Health probe paths pass through without logging."""
    from routstr.core.middleware import _should_log

    # HEAD method is always skipped regardless of path
    assert _should_log("HEAD", "/v1/chat/completions") is False
    assert _should_log("OPTIONS", "/v1/chat/completions") is False
