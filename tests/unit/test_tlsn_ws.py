"""Unit tests for the TLSN channel-B ws proxy (routstr/tlsn_ws.py) and the
/v1/models proverd_ws advertisement.

The mock proverd is a real ``websockets`` server in a background thread; the
proxy endpoint runs under starlette's TestClient portal loop. TCP between the
two loops exercises the actual frame passthrough.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import websockets
from fastapi import FastAPI
from starlette.testclient import TestClient

import routstr.tlsn_ws as tlsn_ws
from routstr.core.settings import settings

SESSION_ID = str(uuid.uuid4())


def receive_close_code(ws: Any) -> int:
    """Read until the close frame; TestClient's raw receive() returns
    ``websocket.close`` as a message instead of raising."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        msg = ws.receive()
        if msg["type"] == "websocket.close":
            return int(msg["code"])
    raise AssertionError("no close frame received within 10s")


class MockProverd:
    """A websockets server in its own thread/loop, scripted per test."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self.port = 0
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            server = await websockets.serve(self._handler, "127.0.0.1", 0)
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.05)
            server.close()
            await server.wait_closed()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(main())

    def __enter__(self) -> "MockProverd":
        self._thread.start()
        assert self._ready.wait(timeout=10), "mock proverd did not start"
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


@pytest.fixture()
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(tlsn_ws.tlsn_ws_router)
    return app


def use_mock_proverd(monkeypatch: pytest.MonkeyPatch, mock: MockProverd) -> None:
    monkeypatch.setattr(settings, "tlsn_proverd_url", f"http://127.0.0.1:{mock.port}")


async def echo_handler(ws: Any) -> None:
    async for frame in ws:
        await ws.send(frame)


def test_proxies_binary_frames_both_ways(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    with MockProverd(echo_handler) as mock:
        use_mock_proverd(monkeypatch, mock)
        client = TestClient(app)
        with client.websocket_connect(f"/v1/tlsn/ws?session_id={SESSION_ID}") as ws:
            payload = b"\x00\x01\x02tlsn-mux-frame\xff" * 100
            ws.send_bytes(payload)
            assert ws.receive_bytes() == payload
            ws.send_text("ping")
            assert ws.receive_text() == "ping"


def test_close_from_upstream_propagates(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def close_after_first(ws: Any) -> None:
        async for _ in ws:
            await ws.close(code=1000)
            return

    with MockProverd(close_after_first) as mock:
        use_mock_proverd(monkeypatch, mock)
        client = TestClient(app)
        with client.websocket_connect(f"/v1/tlsn/ws?session_id={SESSION_ID}") as ws:
            ws.send_bytes(b"x")
            assert receive_close_code(ws) == 1000


def test_close_from_client_closes_upstream(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = threading.Event()

    async def watch_close(ws: Any) -> None:
        try:
            async for _ in ws:
                pass
        finally:
            closed.set()

    with MockProverd(watch_close) as mock:
        use_mock_proverd(monkeypatch, mock)
        client = TestClient(app)
        with client.websocket_connect(f"/v1/tlsn/ws?session_id={SESSION_ID}") as ws:
            ws.send_bytes(b"hello")
            time.sleep(0.2)
        # exiting the context manager disconnects the client ws
        assert closed.wait(timeout=10), "client close never reached proverd"


def test_invalid_session_id_rejected(app: FastAPI) -> None:
    client = TestClient(app)
    with client.websocket_connect("/v1/tlsn/ws?session_id=not-a-uuid") as ws:
        assert receive_close_code(ws) == 1008


def test_proverd_unreachable_closes_1011(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "tlsn_proverd_url", "http://127.0.0.1:1")
    client = TestClient(app)
    with client.websocket_connect(f"/v1/tlsn/ws?session_id={SESSION_ID}") as ws:
        assert receive_close_code(ws) == 1011


def test_session_id_reaches_proverd(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    async def record_path(ws: Any) -> None:
        seen["path"] = ws.path
        await ws.close()

    with MockProverd(record_path) as mock:
        use_mock_proverd(monkeypatch, mock)
        client = TestClient(app)
        with client.websocket_connect(f"/v1/tlsn/ws?session_id={SESSION_ID}") as ws:
            receive_close_code(ws)
        deadline = time.monotonic() + 5
        while "path" not in seen and time.monotonic() < deadline:
            time.sleep(0.05)
        assert seen["path"] == f"/ws?session_id={SESSION_ID}"


# ── /v1/models advertisement ──────────────────────────────────────────────


def test_public_url_prefers_node_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tlsn_proverd_url", "http://proverd:7047")
    monkeypatch.setattr(settings, "http_url", "https://node.example.com")
    assert tlsn_ws.tlsn_proverd_ws_public_url() == "wss://node.example.com/v1/tlsn/ws"


def test_public_url_http_maps_to_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tlsn_proverd_url", "http://proverd:7047")
    monkeypatch.setattr(settings, "http_url", "http://127.0.0.1:8000")
    assert tlsn_ws.tlsn_proverd_ws_public_url() == "ws://127.0.0.1:8000/v1/tlsn/ws"


def test_public_url_falls_back_to_proverd_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tlsn_proverd_url", "http://127.0.0.1:7047/")
    monkeypatch.setattr(settings, "http_url", "")
    assert tlsn_ws.tlsn_proverd_ws_public_url() == "ws://127.0.0.1:7047/ws"


# ── tlsn advertisement: upstream model forwarding ───────��─────────────────


def _advertised_tlsn_block(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_id: str,
    candidate_upstream_id: str | None,
) -> object:
    """Call the /v1/models endpoint with one fake model/provider and return
    its tlsn block. The candidate's id is what the proxy would forward
    upstream (the canonical listing id may differ, e.g. glm-5.3-flash vs
    z-ai/glm-5.3-flash)."""
    import routstr.payment.models as payment_models
    from routstr.payment.models import Architecture, Model, Pricing

    def make_model(mid: str) -> Model:
        return Model(
            id=mid,
            name=mid,
            created=0,
            description="",
            context_length=64_000,
            architecture=Architecture(
                modality="text->text",
                input_modalities=["text"],
                output_modalities=["text"],
                tokenizer="Other",
                instruct_type=None,
            ),
            pricing=Pricing(prompt=0.001, completion=0.002),
        )

    model = make_model(model_id)
    candidate = make_model(candidate_upstream_id or model_id)
    provider = MagicMock()
    provider.provider_type = "ppqai"
    provider.base_url = "https://api.example.com"

    monkeypatch.setattr(settings, "tlsn_proverd_url", "http://proverd:7047")
    monkeypatch.setattr(settings, "http_url", "https://node.example.com")
    monkeypatch.setattr("routstr.proxy.get_unique_models", lambda: [model])
    monkeypatch.setattr("routstr.proxy.get_provider_for_model", lambda _mid: [provider])
    monkeypatch.setattr(
        "routstr.proxy.get_candidates", lambda _mid: [(candidate, provider)]
    )

    import asyncio

    result = asyncio.get_event_loop().run_until_complete(
        payment_models.models(session=MagicMock())
    )
    return result["data"][0].get("tlsn")


def test_advertisement_includes_candidate_upstream_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tlsn = _advertised_tlsn_block(
        monkeypatch,
        model_id="glm-5.3-flash",
        candidate_upstream_id="z-ai/glm-5.3-flash",
    )
    assert isinstance(tlsn, dict)
    assert tlsn["upstream_models"] == ["z-ai/glm-5.3-flash"]
    assert tlsn["upstream_hosts"] == ["api.example.com"]


def test_advertisement_omits_upstream_models_when_ids_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tlsn = _advertised_tlsn_block(
        monkeypatch, model_id="gpt-mock", candidate_upstream_id=None
    )
    assert isinstance(tlsn, dict)
    assert "upstream_models" not in tlsn
