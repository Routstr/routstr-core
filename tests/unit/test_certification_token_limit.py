"""Probes fall back to ``max_completion_tokens`` when ``max_tokens`` is refused.

OpenAI's o-series and gpt-5 reject ``max_tokens`` on chat completions, so a
probe that only ever sends it fails ``usage.capture`` on a healthy upstream.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from routstr.upstream.certification import (
    PROBE_MAX_TOKENS,
    STATUS_OK,
    certify_upstream_url,
)
from routstr.upstream.certification_probe import wants_max_completion_tokens

OPENAI_REJECTION = {
    "error": {
        "message": (
            "Unsupported parameter: 'max_tokens' is not supported with this "
            "model. Use 'max_completion_tokens' instead."
        ),
        "type": "invalid_request_error",
        "param": "max_tokens",
        "code": "unsupported_parameter",
    }
}


@pytest.fixture(autouse=True)
def _restore_price_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """``certify_upstream_url`` publishes ``sats_usd_price`` to the price
    module's globals; restore them so no later test sees this quote."""
    from routstr.payment import price

    monkeypatch.setattr(price, "SATS_USD_PRICE", price.SATS_USD_PRICE)
    monkeypatch.setattr(price, "BTC_USD_PRICE", price.BTC_USD_PRICE)


def _row(result: dict[str, Any], row_id: str) -> dict[str, Any]:
    return next(row for row in result["rows"] if row["id"] == row_id)


@pytest.mark.asyncio
async def test_probe_retries_with_max_completion_tokens() -> None:
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-5"}]})
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json=OPENAI_REJECTION)
        return httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "usage": {"prompt_tokens": 8, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await certify_upstream_url(
            "https://mock.example/v1",
            model_id="gpt-5",
            prompt_price=1e-6,
            completion_price=2e-6,
            sats_usd_price=0.001,
            client=client,
        )

    assert _row(result, "usage.capture")["status"] == STATUS_OK
    assert _row(result, "cost.prompt_completion")["status"] == STATUS_OK
    # Rejected probe, retried probe, then both cache-probe calls reuse the
    # accepted field instead of being rejected again.
    assert ["max_tokens" in body for body in bodies] == [True, False, False, False]
    assert all(
        body.get("max_completion_tokens") == PROBE_MAX_TOKENS for body in bodies[1:]
    )


@pytest.mark.asyncio
async def test_probe_does_not_retry_unrelated_400() -> None:
    calls: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "model not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await certify_upstream_url(
            "https://mock.example/v1",
            model_id="m",
            prompt_price=1e-6,
            completion_price=2e-6,
            sats_usd_price=0.001,
            client=client,
        )

    assert len(calls) == 1
    assert _row(result, "usage.capture")["status"] != STATUS_OK


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (400, OPENAI_REJECTION, True),
        (400, {"error": {"message": "bad model"}}, False),
        (422, OPENAI_REJECTION, False),
        (200, OPENAI_REJECTION, False),
        (400, None, False),
        (None, None, False),
    ],
)
def test_wants_max_completion_tokens(
    status: int | None, payload: Any, expected: bool
) -> None:
    assert wants_max_completion_tokens(status, payload) is expected
