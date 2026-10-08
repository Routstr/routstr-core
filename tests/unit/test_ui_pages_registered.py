"""Every static UI page must be served on a direct load, not the proxy 404."""

from __future__ import annotations

from pathlib import Path

from routstr.core import main as core_main

UI_APP_DIR = Path(__file__).resolve().parents[2] / "ui" / "app"


def test_every_ui_app_page_is_in_ui_pages() -> None:
    routes = {
        page.parent.relative_to(UI_APP_DIR).as_posix()
        for page in UI_APP_DIR.rglob("page.tsx")
        if page.parent != UI_APP_DIR
    }
    assert routes, "no ui/app pages found"
    assert routes - set(core_main.UI_PAGES) == set()
