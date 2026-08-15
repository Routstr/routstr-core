import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from routstr.core.exceptions import http_exception_handler


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
        }
    )
    request.state.request_id = "req-123"
    return request


@pytest.mark.asyncio
async def test_structured_http_error_uses_standard_error_envelope() -> None:
    request = _request()
    error = {
        "message": "Cashu mint is unreachable",
        "type": "mint_unreachable",
        "code": "cashu_mint_unreachable",
        "details": {"mint": "https://mint.example"},
    }

    response = await http_exception_handler(
        request,
        HTTPException(status_code=503, detail={"error": error}),
    )

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "detail": {"error": error},
        "error": error,
        "request_id": "req-123",
    }


@pytest.mark.asyncio
async def test_plain_http_error_keeps_detail_envelope() -> None:
    response = await http_exception_handler(
        _request(),
        HTTPException(status_code=404, detail="Not found"),
    )

    assert json.loads(response.body) == {
        "detail": "Not found",
        "request_id": "req-123",
    }
