"""Tests for the built-in 404 handler in routstr.proxy."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routstr import proxy
from routstr.proxy import proxy_router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(proxy_router)
    return app


@pytest.mark.skipif(
    proxy._NOT_FOUND_HTML is None,
    reason="UI bundle (ui_out/404.html) not present in this environment",
)
def test_unknown_path_returns_html_404_for_browser() -> None:
    client = TestClient(_make_app())
    response = client.get("/some/random/page", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "404" in response.text


def test_unknown_path_returns_json_404_for_api_client() -> None:
    client = TestClient(_make_app())
    response = client.get("/some/random/page", headers={"accept": "application/json"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["error"]["type"] == "not_found"
    assert payload["error"]["code"] == 404
    assert "/some/random/page" in payload["error"]["message"]


def test_root_path_returns_404_for_proxy_router() -> None:
    client = TestClient(_make_app())
    response = client.get("/", headers={"accept": "application/json"})
    assert response.status_code == 404


def test_v1_path_is_not_intercepted_by_404_handler() -> None:
    """Paths starting with v1/ must reach the proxy logic, not the 404 handler."""
    client = TestClient(_make_app(), raise_server_exceptions=False)
    response = client.get("/v1/anything")
    if response.status_code == 404:
        # Any 404 here must come from inner proxy logic, not our HTML page.
        assert "<!DOCTYPE html>" not in response.text


def test_json_returned_when_ui_html_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "_NOT_FOUND_HTML", None)
    client = TestClient(_make_app())
    response = client.get("/some/random/page", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
