"""Shared 404 handler used by the proxy catch-all and tests."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

_NOT_FOUND_HTML_FILE = Path(__file__).parent.parent.parent / "ui_out" / "404.html"


def _read_not_found_html() -> str | None:
    try:
        return _NOT_FOUND_HTML_FILE.read_text(encoding="utf-8")
    except OSError:
        return None


_NOT_FOUND_HTML: str | None = _read_not_found_html()


def build_not_found_response(request: Request, path: str) -> Response:
    """Return a 404 response.

    HTML 404 page only for GET requests from browsers (Accept: text/html).
    All POST requests and API clients receive a JSON 404.
    """
    accept = request.headers.get("accept", "").lower()
    prefers_html = (
        request.method == "GET"
        and "text/html" in accept
        and "application/json" not in accept
    )
    request_id = getattr(request.state, "request_id", "unknown")

    if prefers_html and _NOT_FOUND_HTML is not None:
        return HTMLResponse(content=_NOT_FOUND_HTML, status_code=404)

    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "message": f"Path '/{path}' not found",
                "type": "not_found",
                "code": 404,
            },
            "request_id": request_id,
        },
    )


async def not_found_catch_all(request: Request, path: str) -> Response:
    """ASGI handler form of :func:`build_not_found_response`."""
    return build_not_found_response(request, path)
