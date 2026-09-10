"""Guards for the pre-auth image URL fetch used by cost estimation."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Iterator

import pytest

from routstr.payment import helpers
from routstr.payment.helpers import (
    IMAGE_FETCH_MAX_BYTES,
    IMAGE_FETCH_MAX_PER_REQUEST,
    _fetch_image_from_url,
    _is_blocked_address,
    _validated_fetch_target,
    estimate_image_tokens_in_messages,
)

REQUESTED_PATHS: list[str] = []


class _Loop:
    def __init__(self, getaddrinfo: Callable[..., Any]) -> None:
        self.getaddrinfo = getaddrinfo


class _Sink(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        REQUESTED_PATHS.append(self.path)
        body = b"x" * (IMAGE_FETCH_MAX_BYTES * 4)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            # The client stops reading once the byte cap is reached.
            pass

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def sink() -> Iterator[str]:
    REQUESTED_PATHS.clear()
    server = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_loopback_url_is_not_fetched(sink: str) -> None:
    assert await _fetch_image_from_url(f"{sink}/internal") is None
    assert REQUESTED_PATHS == []


@pytest.mark.asyncio
async def test_link_local_metadata_url_is_not_fetched() -> None:
    assert await _fetch_image_from_url("http://169.254.169.254/latest/meta-data") is None


@pytest.mark.asyncio
async def test_non_http_scheme_is_rejected() -> None:
    assert await _fetch_image_from_url("file:///etc/passwd") is None


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",  # CGNAT: reachable inside many hosting networks
        "192.0.0.1",
        "224.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "2002:7f00:1::",  # 6to4 wrapping 127.0.0.1
    ],
)
def test_non_global_addresses_are_blocked(address: str) -> None:
    assert _is_blocked_address(address) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "2001:4860:4860::8888"])
def test_global_addresses_are_allowed(address: str) -> None:
    assert _is_blocked_address(address) is False


@pytest.mark.asyncio
async def test_http_target_is_pinned_to_validated_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_getaddrinfo(*args: object, **kwargs: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    monkeypatch.setattr(
        helpers.asyncio, "get_running_loop", lambda: _Loop(fake_getaddrinfo)
    )

    target, host_header = await _validated_fetch_target("http://example.com/cat.png")

    assert target == "http://93.184.216.34/cat.png"
    assert host_header == "example.com"


@pytest.mark.asyncio
async def test_https_target_keeps_hostname_for_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_getaddrinfo(*args: object, **kwargs: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(
        helpers.asyncio, "get_running_loop", lambda: _Loop(fake_getaddrinfo)
    )

    target, host_header = await _validated_fetch_target("https://example.com/cat.png")

    assert target == "https://example.com/cat.png"
    assert host_header == "example.com"


@pytest.fixture
def reachable_sink(sink: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Let the local sink stand in for a public host, so cap tests keep the
    address validation intact instead of disabling it."""

    async def passthrough(url: str) -> tuple[str, str]:
        return url, "images.example.com"

    monkeypatch.setattr(helpers, "_validated_fetch_target", passthrough)
    return sink


@pytest.mark.asyncio
async def test_downloaded_bytes_are_capped(reachable_sink: str) -> None:
    body = await _fetch_image_from_url(f"{reachable_sink}/allowed")

    assert body is not None
    assert len(body) <= IMAGE_FETCH_MAX_BYTES
    assert REQUESTED_PATHS == ["/allowed"]


@pytest.mark.asyncio
async def test_url_fetches_are_capped_per_request(reachable_sink: str) -> None:
    urls = IMAGE_FETCH_MAX_PER_REQUEST + 3
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"{reachable_sink}/{index}"}}
                for index in range(urls)
            ],
        }
    ]

    await estimate_image_tokens_in_messages(messages)

    assert len(REQUESTED_PATHS) == IMAGE_FETCH_MAX_PER_REQUEST
