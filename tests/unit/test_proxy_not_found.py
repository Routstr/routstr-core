"""Tests for the app-level 404 handler in routstr.core.main."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routstr.core import main as core_main


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_api_route(
        "/{path:path}",
        core_main.not_found_catch_all,
        methods=["GET", "POST"],
        include_in_schema=False,
    )
    return app


@pytest.mark.skipif(
    core_main._NOT_FOUND_HTML is None,
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


def test_root_path_returns_404() -> None:
    client = TestClient(_make_app())
    response = client.get("/", headers={"accept": "application/json"})
    assert response.status_code == 404


def test_json_returned_when_ui_html_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from routstr.core import not_found as nf

    monkeypatch.setattr(nf, "_NOT_FOUND_HTML", None)
    client = TestClient(_make_app())
    response = client.get("/some/random/page", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_post_unknown_path_returns_json_even_for_browser() -> None:
    client = TestClient(_make_app())
    response = client.post("/some/random/page", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["error"]["type"] == "not_found"
