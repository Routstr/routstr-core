"""The bytes Routstr sends Venice for a web-search request, captured past
litellm's Anthropic adapter where the ``web_search_options`` 400 arose."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator

import pytest

from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.litellm_routing import configure_litellm
from routstr.upstream.venice import VeniceUpstreamProvider

_CHUNKS = [
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-v4-flash-0731",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}}],
    },
    {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-v4-flash-0731",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    },
]


@pytest.fixture
def upstream() -> Iterator[tuple[str, dict[str, Any]]]:
    """A loopback stand-in for ``api.venice.ai`` that records one request."""
    captured: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
            length = int(self.headers.get("Content-Length", 0))
            captured["path"] = self.path
            captured["body"] = json.loads(self.rfile.read(length))

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in _CHUNKS:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *args: Any) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", captured
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _model() -> Model:
    return Model(
        id="deepseek-v4-flash-0731",
        name="deepseek-v4-flash-0731",
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="Unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.0, completion=0.0),
    )


@pytest.mark.asyncio
async def test_web_search_request_reaches_venice_in_its_own_shape(
    upstream: tuple[str, dict[str, Any]],
) -> None:
    base_url, captured = upstream
    # The app applies this at startup; without it litellm posts the Anthropic
    # body to /responses, which Venice serves only in alpha.
    configure_litellm()

    provider = VeniceUpstreamProvider(api_key="sk-test")
    provider.base_url = base_url

    await provider._dispatch_anthropic_messages(
        request_body=json.dumps(
            {
                "model": "venice/deepseek-v4-flash-0731",
                "messages": [{"role": "user", "content": "what shipped today?"}],
                "max_tokens": 64,
                "stream": True,
                "tools": [
                    {"type": "web_search_20250305", "name": "web_search"},
                    {
                        "name": "lookup",
                        "description": "Look something up",
                        "input_schema": {"type": "object", "properties": {}},
                    },
                ],
            }
        ).encode(),
        model_obj=_model(),
    )

    body = captured["body"]
    assert captured["path"] == "/v1/chat/completions"
    assert "web_search_options" not in body
    assert body["model"] == (
        "deepseek-v4-flash-0731:enable_web_search=auto&enable_web_citations=true"
    )
    assert [tool["function"]["name"] for tool in body["tools"]] == ["lookup"]
