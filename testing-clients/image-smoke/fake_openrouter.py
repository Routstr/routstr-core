#!/usr/bin/env python3
"""Disposable, dependency-free fake OpenRouter. Never contacts the network."""

import base64
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

MODEL = "recraft/recraft-v4.1-flash"
# Valid 1x1 PNG, not a generated image.
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
PARAMETERS = {
    "aspect_ratio": {
        "type": "enum",
        "values": ["1:1", "4:3", "3:4", "16:9", "9:16", "auto"],
    },
    "n": {"type": "range", "min": 1, "max": 6},
}
ENDPOINT = {
    "provider_name": "Recraft",
    "provider_slug": "recraft",
    "provider_tag": "recraft",
    "supported_parameters": PARAMETERS,
    "allowed_passthrough_parameters": [],
    "supports_streaming": False,
    "pricing": [{"billable": "output_image", "unit": "image", "cost_usd": 0.007}],
}
GENERAL_MODEL = {
    "id": MODEL,
    "name": "Fake Recraft V4.1 Flash",
    "created": 0,
    "description": "Local smoke fixture, not a real generator",
    "context_length": 0,
    "architecture": {
        "modality": "text->image",
        "input_modalities": ["text"],
        "output_modalities": ["image"],
        "tokenizer": "unknown",
        "instruct_type": None,
    },
    "pricing": {"prompt": "0", "completion": "0", "image_output": "0.007"},
}
IMAGE_MODEL = {
    "id": MODEL,
    "name": GENERAL_MODEL["name"],
    "created": 0,
    "description": GENERAL_MODEL["description"],
    "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
    "supported_parameters": PARAMETERS,
    "supports_streaming": False,
    "endpoints": f"/api/v1/images/models/{MODEL}/endpoints",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # Never log authorization, prompts, image bytes, or payment tokens.
        pass

    def reply(self, status: int, payload: dict[str, Any]) -> None:
        content = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/")
        if path == "/health":
            self.reply(200, {"status": "ok", "fake": True})
        elif path == "/api/v1/models":
            # Match OpenRouter's text-only default discovery contract.
            from urllib.parse import parse_qs

            modalities = (
                parse_qs(parsed.query).get("output_modalities", ["text"])[0].split(",")
            )
            self.reply(
                200,
                {
                    "data": [GENERAL_MODEL]
                    if {"image", "all"}.intersection(modalities)
                    else []
                },
            )
        elif path == "/api/v1/embeddings/models":
            self.reply(200, {"data": []})
        elif path == "/api/v1/images/models":
            self.reply(200, {"data": [IMAGE_MODEL]})
        elif path == f"/api/v1/images/models/{MODEL}/endpoints":
            self.reply(200, {"id": MODEL, "endpoints": [ENDPOINT]})
        else:
            self.reply(
                404, {"error": {"message": "Unknown fake endpoint", "code": 404}}
            )

    def do_POST(self) -> None:
        if self.path != "/api/v1/images":
            return self.reply(
                404, {"error": {"message": "Unknown fake endpoint", "code": 404}}
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                return self.reply(
                    413, {"error": {"message": "Body limit", "code": 413}}
                )
            body = json.loads(self.rfile.read(length))
            n = body.get("n", 1)
            if (
                body.get("model") != MODEL
                or not isinstance(body.get("prompt"), str)
                or not body["prompt"].strip()
                or body.get("stream", False) is not False
                or isinstance(n, bool)
                or not isinstance(n, int)
                or not 1 <= n <= 6
            ):
                raise ValueError("Invalid image request")
            provider = body.get("provider")
            if provider is not None and provider != {
                "only": ["recraft"],
                "allow_fallbacks": False,
            }:
                raise ValueError("Invalid routing")
        except (ValueError, TypeError, AttributeError):
            return self.reply(
                400, {"error": {"message": "Invalid image request", "code": 400}}
            )
        self.reply(
            200,
            {
                "created": int(time.time()),
                "data": [
                    {"b64_json": PNG, "media_type": "image/png"} for _ in range(n)
                ],
                # Deliberately cost-only: zero tokens must not mean free images.
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost": round(0.007 * n, 6),
                },
            },
        )


if __name__ == "__main__":
    assert base64.b64decode(PNG).startswith(b"\x89PNG\r\n\x1a\n")
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
