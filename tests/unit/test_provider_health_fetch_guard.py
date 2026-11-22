"""Destination checks on the URLs fetch_provider_health takes from announcements."""

import socket
from typing import Any, Callable

import httpx
import pytest

from routstr.nostr import discovery
from routstr.nostr.discovery import fetch_provider_health

PUBLIC_ADDRESS = "93.184.216.34"

Route = Callable[[httpx.Request], httpx.Response]


class _Upstream:
    """Stands in for the network: records every request the node sends."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.proxies: list[Any] = []
        self.routes: dict[str, Route] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(
            f"{request.url.scheme}://{request.url.host}{request.url.path}"
        )
        return route(request) if route else httpx.Response(404)

    @property
    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> _Upstream:
    recorder = _Upstream()
    real_client = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        # A proxy mount would take precedence over the mock transport.
        recorder.proxies.append(kwargs.pop("proxy", None))
        return real_client(transport=httpx.MockTransport(recorder.handle), **kwargs)

    monkeypatch.setattr(discovery.httpx, "AsyncClient", client)
    return recorder


def _resolve(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[str], str]
) -> list[str]:
    lookups: list[str] = []

    def fake_getaddrinfo(host: str, port: int, *args: object) -> list[tuple]:
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer(host), port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return lookups


def _json(payload: dict[str, Any]) -> Route:
    return lambda request: httpx.Response(200, json=payload)


def _redirect(location: str) -> Route:
    return lambda request: httpx.Response(302, headers={"Location": location})


@pytest.mark.asyncio
async def test_rebinding_name_is_requested_at_the_checked_address(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([PUBLIC_ADDRESS])
    _resolve(monkeypatch, lambda host: next(answers, "127.0.0.1"))

    health = await fetch_provider_health("http://rebind.example")

    # The first lookup was public and the request went to that address; the
    # later lookups rebound to loopback and nothing was sent.
    assert health["endpoint"] == "error"
    assert upstream.hosts == [PUBLIC_ADDRESS]
    assert upstream.requests[0].headers["Host"] == "rebind.example"


@pytest.mark.asyncio
async def test_redirect_to_internal_address_is_refused(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolve(monkeypatch, lambda host: host if host == "127.0.0.1" else PUBLIC_ADDRESS)
    redirect = _redirect("http://127.0.0.1:8080/admin")
    for path in ("/v1/info", "/v1/models", "/"):
        upstream.routes[f"http://{PUBLIC_ADDRESS}{path}"] = redirect

    health = await fetch_provider_health("http://provider.example")

    assert health["endpoint"] == "error"
    assert "127.0.0.1" not in upstream.hosts


@pytest.mark.asyncio
async def test_redirects_resolve_against_announced_hostname(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolve(monkeypatch, lambda host: PUBLIC_ADDRESS)
    upstream.routes[f"http://{PUBLIC_ADDRESS}/v1/info"] = _redirect("/moved")
    upstream.routes[f"http://{PUBLIC_ADDRESS}/moved"] = _redirect(
        "https://provider.example/v1/info"
    )
    upstream.routes["https://provider.example/v1/info"] = _json({"name": "p"})

    health = await fetch_provider_health("http://provider.example")

    assert health == {"status_code": 200, "endpoint": "info", "json": {"name": "p"}}
    assert [str(request.url) for request in upstream.requests] == [
        f"http://{PUBLIC_ADDRESS}/v1/info",
        f"http://{PUBLIC_ADDRESS}/moved",
        "https://provider.example/v1/info",
    ]
    assert [request.headers["Host"] for request in upstream.requests] == [
        "provider.example"
    ] * 3


@pytest.mark.asyncio
async def test_idn_host_is_checked_and_sent_in_its_punycode_form(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookups = _resolve(monkeypatch, lambda host: PUBLIC_ADDRESS)
    upstream.routes["https://straße.example/v1/info"] = _json({"name": "p"})

    health = await fetch_provider_health("https://Straße.example:8443")

    # IDNA 2008, as httpx connects; Python's IDNA 2003 codec would give
    # "strasse.example" and check a different name.
    assert health == {"status_code": 200, "endpoint": "info", "json": {"name": "p"}}
    assert lookups == ["xn--strae-oqa.example"]
    assert upstream.requests[0].headers["Host"] == "xn--strae-oqa.example:8443"


@pytest.mark.asyncio
async def test_onion_in_query_does_not_skip_the_check(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookups = _resolve(monkeypatch, lambda host: "127.0.0.1")

    health = await fetch_provider_health("http://evil.example/?x=.onion")

    assert health["endpoint"] == "error"
    assert upstream.proxies == [None]
    assert upstream.requests == []
    assert "evil.example" in lookups


@pytest.mark.asyncio
async def test_onion_endpoint_uses_proxy_without_local_lookup(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookups = _resolve(monkeypatch, lambda host: "127.0.0.1")
    upstream.routes["http://provider.onion/v1/info"] = _json({"name": "p"})

    health = await fetch_provider_health("http://provider.onion")

    assert health["endpoint"] == "info"
    assert upstream.proxies[0] is not None
    assert upstream.hosts == ["provider.onion"]
    assert lookups == []


@pytest.mark.asyncio
async def test_onion_redirect_off_onion_is_refused(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    _resolve(monkeypatch, lambda host: PUBLIC_ADDRESS)
    redirect = _redirect("http://127.0.0.1:8080/admin")
    for path in ("/v1/info", "/v1/models", "/"):
        upstream.routes[f"http://provider.onion{path}"] = redirect

    health = await fetch_provider_health("http://provider.onion")

    assert health["endpoint"] == "error"
    assert set(upstream.hosts) == {"provider.onion"}
