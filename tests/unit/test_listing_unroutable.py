"""The node never announces endpoints other users cannot reach."""

import pytest

from routstr.core.settings import settings
from routstr.nostr import listing


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://10.0.0.5",
        "http://192.168.1.10:8000",
        "http://[::1]:8000",
        "http://169.254.1.1",
        "http://node.localhost",
    ],
)
def test_unroutable_urls_are_not_announced(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "http_url", url)
    monkeypatch.setattr(settings, "onion_url", "")
    monkeypatch.setattr(listing, "discover_onion_url_from_tor", lambda: None)
    assert listing._resolve_endpoint_urls() == []


def test_public_url_is_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "http_url", "https://node.example.com")
    monkeypatch.setattr(settings, "onion_url", "")
    monkeypatch.setattr(listing, "discover_onion_url_from_tor", lambda: None)
    assert listing._resolve_endpoint_urls() == ["https://node.example.com"]
