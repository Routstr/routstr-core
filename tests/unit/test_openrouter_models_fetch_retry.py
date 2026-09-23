"""Retry behaviour for the OpenRouter catalogue fetch."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from routstr.payment import models as models_module
from routstr.payment.models import async_fetch_openrouter_models

MODELS_URL = "https://openrouter.ai/api/v1/models"
EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings/models"


def _model(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id,
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
    }


def _ok_response(url: str, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("GET", url),
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )


def _error_response(url: str, status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", url), content=b"nope")


def _truncated_response(url: str) -> httpx.Response:
    """A body cut mid-JSON — the shape OpenRouter actually sent the node."""
    return httpx.Response(
        200,
        request=httpx.Request("GET", url),
        content=b'{"data": [{"id": "vendor/model-a", "name": "Model A", "pric',
        headers={"content-type": "application/json"},
    )


def _payload_for(url: str) -> dict[str, Any]:
    if url.endswith("/embeddings/models"):
        return {"data": [_model("vendor/embed-1")]}
    return {"data": [_model("vendor/model-a")]}


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models_module, "OPENROUTER_MODELS_RETRY_BACKOFF_SECONDS", 0)


def _install_get(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[str, int], httpx.Response],
) -> dict[str, int]:
    """Patch ``httpx.AsyncClient.get`` and count calls per endpoint."""
    counts: dict[str, int] = {}

    async def fake_get(
        self: httpx.AsyncClient, url: Any, **kwargs: Any
    ) -> httpx.Response:
        key = str(url)
        counts[key] = counts.get(key, 0) + 1
        return handler(key, counts[key])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return counts


@pytest.mark.asyncio
async def test_truncated_body_is_retried_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated body on the first attempt must not empty the catalogue."""

    def handler(url: str, call: int) -> httpx.Response:
        if call == 1:
            return _truncated_response(url)
        return _ok_response(url, _payload_for(url))

    counts = _install_get(monkeypatch, handler)

    result = await async_fetch_openrouter_models()

    assert [model["id"] for model in result] == ["vendor/model-a", "vendor/embed-1"]
    assert counts[MODELS_URL] == 2
    assert counts[EMBEDDINGS_URL] == 2


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts_and_logs_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After every attempt fails: log once and return an empty catalogue."""
    errors: list[str] = []
    monkeypatch.setattr(models_module.logger, "error", lambda msg: errors.append(msg))

    counts = _install_get(monkeypatch, lambda url, call: _truncated_response(url))

    result = await async_fetch_openrouter_models()

    assert result == []
    assert counts[MODELS_URL] == models_module.OPENROUTER_MODELS_MAX_ATTEMPTS
    assert len(errors) == 1
    assert "after 3 attempt(s)" in errors[0]


@pytest.mark.asyncio
async def test_embeddings_outage_still_yields_the_main_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The secondary endpoint is best-effort: it cannot empty the catalogue."""

    def handler(url: str, call: int) -> httpx.Response:
        if url == EMBEDDINGS_URL:
            return _error_response(url, 503)
        return _ok_response(url, _payload_for(url))

    counts = _install_get(monkeypatch, handler)

    result = await async_fetch_openrouter_models()

    assert [model["id"] for model in result] == ["vendor/model-a"]
    assert counts[MODELS_URL] == 1
    assert counts[EMBEDDINGS_URL] == 1


@pytest.mark.asyncio
async def test_main_catalogue_server_error_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx on /models is transient, so the attempt is retried."""

    def handler(url: str, call: int) -> httpx.Response:
        if url == MODELS_URL and call == 1:
            return _error_response(url, 503)
        return _ok_response(url, _payload_for(url))

    counts = _install_get(monkeypatch, handler)

    result = await async_fetch_openrouter_models()

    assert [model["id"] for model in result] == ["vendor/model-a", "vendor/embed-1"]
    assert counts[MODELS_URL] == 2


@pytest.mark.asyncio
async def test_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying a 4xx only adds load to an upstream that already said no."""
    counts = _install_get(monkeypatch, lambda url, call: _error_response(url, 401))

    result = await async_fetch_openrouter_models()

    assert result == []
    assert counts[MODELS_URL] == 1


@pytest.mark.asyncio
async def test_source_filter_and_free_models_are_still_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The moved filter loop still strips prefixes and drops free tiers."""
    payload = {
        "data": [
            _model("openai/gpt-x"),
            _model("openai/gpt-x:free"),
            _model("other/y"),
        ]
    }

    def handler(url: str, call: int) -> httpx.Response:
        return _ok_response(url, payload if url == MODELS_URL else {"data": []})

    _install_get(monkeypatch, handler)

    result = await async_fetch_openrouter_models(source_filter="openai")

    assert [model["id"] for model in result] == ["gpt-x"]
