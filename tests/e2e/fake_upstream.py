"""Minimal OpenAI-compatible upstream for process-level tests.

Requests whose ``user`` field is ``"slow"`` hang for ``FAKE_UPSTREAM_DELAY``
seconds, standing in for a long generation that a restart interrupts; ``"greedy"``
reports more usage than the payment covers, so all of it is charged. Every
accepted completion is appended to ``FAKE_UPSTREAM_LOG`` so tests can tell when
a request reached the upstream.
"""

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

MODEL = "e2e-model"
DELAY = float(os.environ.get("FAKE_UPSTREAM_DELAY", "120"))
LOG = os.environ.get("FAKE_UPSTREAM_LOG")


def _record(path: str, body: dict) -> None:
    if LOG:
        with open(LOG, "a") as log:
            log.write(json.dumps({"path": path, "user": body.get("user")}) + "\n")


async def models(_: Request) -> Response:
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": MODEL,
                    "object": "model",
                    "created": 1,
                    "owned_by": "e2e",
                    "context_length": 8192,
                    "pricing": {"prompt": "0", "completion": "0", "request": "0"},
                }
            ],
        }
    )


def _usage(body: dict) -> dict:
    completion = 100_000 if body.get("user") == "greedy" else 2
    return {
        "prompt_tokens": 3,
        "completion_tokens": completion,
        "total_tokens": 3 + completion,
    }


async def chat(request: Request) -> Response:
    body = await request.json()
    _record("chat", body)
    usage = _usage(body)
    if body.get("user") == "slow":
        await asyncio.sleep(DELAY)
    if body.get("stream"):

        async def events() -> AsyncIterator[str]:
            chunk = {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [{"index": 0, "delta": {"content": "hi"}}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            final = {**chunk, "choices": [], "usage": usage}
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")
    return JSONResponse(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
    )


async def responses(request: Request) -> Response:
    body = await request.json()
    _record("responses", body)
    if body.get("user") == "slow":
        await asyncio.sleep(DELAY)
    return JSONResponse(
        {
            "id": "r1",
            "object": "response",
            "created_at": int(time.time()),
            "model": MODEL,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hi"}],
                }
            ],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }
    )


app = Starlette(
    routes=[
        Route("/v1/models", models),
        Route("/v1/chat/completions", chat, methods=["POST"]),
        Route("/v1/responses", responses, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
