"""Unit tests for the TLSN verified-mode path (routstr/upstream/tlsn_verified.py).

The proverd HTTP call is faked with an httpx MockTransport; payment is
patched out (covered by existing billing tests).
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import routstr.upstream.tlsn_verified as tlsn_verified
from routstr.core.exceptions import UpstreamError
from routstr.core.settings import settings
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.generic import GenericUpstreamProvider

MODEL = Model(
    id="gpt-test",
    name="gpt-test",
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
    sats_pricing=Pricing(prompt=0.001, completion=0.002),
)

CHAT_BODY = json.dumps(
    {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]}
).encode()

UPSTREAM_JSON = json.dumps(
    {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "gpt-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
).encode()

COST_DATA = {
    "total_msats": 42,
    "charged_msats": 42,
    "input_msats": 30,
    "output_msats": 12,
    "total_usd": 0.0,
}


def make_request(
    headers: dict[str, str] | None = None,
    method: str = "POST",
    query_params: dict[str, str] | None = None,
) -> MagicMock:
    request = MagicMock()
    request.headers = headers or {}
    request.method = method
    request.query_params = query_params or {}
    return request


def make_provider() -> GenericUpstreamProvider:
    return GenericUpstreamProvider(
        base_url="https://api.upstream.test", api_key="upstream-secret-key"
    )


def make_fake_proverd(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    status: int = 200,
    body: bytes = UPSTREAM_JSON,
) -> None:
    """Fake proverd as a raw ASGI app (streams properly, unlike MockTransport)."""

    async def asgi(scope: dict, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        captured["url"] = f"http://proverd.test{scope['path']}"
        request_body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            request_body += message.get("body", b"")
            if not message.get("more_body"):
                break
        captured["payload"] = json.loads(request_body)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi), base_url="http://proverd.test"
    )
    monkeypatch.setattr(tlsn_verified, "_proverd_client", lambda: client)


async def call_forward(
    request: MagicMock,
    provider: GenericUpstreamProvider,
    body: bytes | None = CHAT_BODY,
) -> Any:
    return await tlsn_verified.forward_verified_via_proverd(
        provider=provider,
        request=request,
        path="v1/chat/completions",
        headers={
            "Authorization": f"Bearer {provider.api_key}",
            "content-type": "application/json",
        },
        request_body=body,
        key=MagicMock(hashed_key="deadbeefcafe"),
        max_cost_for_model=1000,
        session=MagicMock(),
        model_obj=MODEL,
        reservation_snapshot=None,
        url="https://api.upstream.test/v1/chat/completions",
        original_model_id="gpt-test",
    )


@pytest.fixture(autouse=True)
def patch_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tlsn_verified,
        "adjust_payment_for_tokens",
        AsyncMock(return_value=COST_DATA),
    )


@pytest.fixture(autouse=True)
def patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tlsn_proverd_url", "http://proverd.test")


@pytest.mark.asyncio
async def test_fails_loud_when_proverd_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tlsn_proverd_url", "")
    request = make_request({"x-routstr-verify": "tlsn-proxy", "x-routstr-tlsn-session": "s1"})
    with pytest.raises(UpstreamError) as exc_info:
        await call_forward(request, make_provider())
    assert exc_info.value.status_code == 400
    assert "TLSN_PROVERD_URL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fails_loud_when_session_header_missing() -> None:
    request = make_request({"x-routstr-verify": "tlsn-proxy"})
    with pytest.raises(UpstreamError) as exc_info:
        await call_forward(request, make_provider())
    assert exc_info.value.status_code == 400
    assert "x-routstr-tlsn-session" in str(exc_info.value)


def make_fake_proverd_sse(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    events: list[bytes],
) -> None:
    """Fake proverd streaming canned SSE events (raw ASGI)."""

    async def asgi(scope: dict, receive: Any, send: Any) -> None:
        request_body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            request_body += message.get("body", b"")
            if not message.get("more_body"):
                break
        captured["payload"] = json.loads(request_body)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        for event in events:
            await send({"type": "http.response.body", "body": event, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi), base_url="http://proverd.test"
    )
    monkeypatch.setattr(tlsn_verified, "_proverd_client", lambda: client)


@pytest.mark.asyncio
async def test_streaming_passthrough_and_post_stream_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage_chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "gpt-test",
        "choices": [],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    events = [
        b'data: {"id":"chatcmpl-1","model":"gpt-test","choices":[{"delta":{"content":"he"}}]}\n\n',
        b'data: {"id":"chatcmpl-1","model":"gpt-test","choices":[{"delta":{"content":"llo"}}]}\n\n',
        f"data: {json.dumps(usage_chunk)}\n\n".encode(),
        b"data: [DONE]\n\n",
    ]
    expected_bytes = b"".join(events)

    captured: dict[str, Any] = {}
    make_fake_proverd_sse(monkeypatch, captured, events)

    # Payment settlement runs post-stream on a fresh DB session.
    payment = AsyncMock(return_value=COST_DATA)
    monkeypatch.setattr(tlsn_verified, "adjust_payment_for_tokens", payment)
    fake_session = MagicMock()
    fake_session.get = AsyncMock(return_value=MagicMock(hashed_key="deadbeefcafe"))

    class _FakeSessionCM:
        async def __aenter__(self) -> Any:
            return fake_session

        async def __aexit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(tlsn_verified, "create_session", lambda: _FakeSessionCM())

    request = make_request(
        {"x-routstr-verify": "tlsn-proxy", "x-routstr-tlsn-session": "sess-sse"}
    )
    streaming_body = json.dumps(
        {
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    ).encode()
    response = await call_forward(request, make_provider(), body=streaming_body)

    assert response.status_code == 200
    assert response.headers["x-routstr-verified"] == "tlsn-proxy"
    assert response.headers["x-routstr-tlsn-session"] == "sess-sse"

    # Consume the stream: bytes must be exactly what proverd sent.
    received = b"".join([chunk async for chunk in response.body_iterator])
    assert received == expected_bytes

    # Billing settled from the captured usage chunk (not an estimate).
    assert payment.await_count == 1
    payload_json = payment.await_args.args[1]
    assert payload_json["usage"] == usage_chunk["usage"]
    assert payload_json["model"] == "gpt-test"


@pytest.mark.asyncio
async def test_non_streaming_byte_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    make_fake_proverd(monkeypatch, captured)

    request = make_request(
        {"x-routstr-verify": "tlsn-proxy", "x-routstr-tlsn-session": "sess-42"}
    )
    response = await call_forward(request, make_provider())

    # --- payload shape sent to proverd ---
    payload = captured["payload"]
    assert captured["url"] == "http://proverd.test/sessions"
    assert payload["session_id"] == "sess-42"
    assert payload["server_name"] == "api.upstream.test"
    assert payload["port"] == 443
    assert payload["request"]["method"] == "POST"
    assert payload["request"]["path"] == "/v1/chat/completions"
    assert payload["redact"] == ["authorization"]
    headers = {k.lower(): v for k, v in payload["request"]["headers"]}
    assert headers["authorization"] == "Bearer upstream-secret-key"
    assert headers["accept-encoding"] == "identity"
    # control headers never leak upstream
    assert "x-routstr-verify" not in headers
    assert "x-routstr-tlsn-session" not in headers
    assert json.loads(payload["request"]["body"])["messages"][0]["content"] == "hi"

    # --- response: byte-passthrough + verified headers + cost headers ---
    assert response.status_code == 200
    assert response.body == UPSTREAM_JSON  # exact bytes, no cost injection
    assert b"remaining_balance_msats" not in response.body
    assert b'"cost"' not in response.body
    assert response.headers["x-routstr-verified"] == "tlsn-proxy"
    assert response.headers["x-routstr-upstream-host"] == "api.upstream.test"
    assert response.headers["x-routstr-tlsn-session"] == "sess-42"
    assert response.headers["x-routstr-cost-msats"] == "42"
    assert response.headers["x-routstr-input-cost-msats"] == "30"
    assert response.headers["x-routstr-output-cost-msats"] == "12"


@pytest.mark.asyncio
async def test_upstream_error_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error_body = json.dumps(
        {"error": {"message": "bad key", "type": "invalid_request_error"}}
    ).encode()
    captured: dict[str, Any] = {}
    make_fake_proverd(monkeypatch, captured, status=401, body=error_body)

    sentinel = MagicMock(name="error_response")
    provider = make_provider()
    provider.forward_upstream_error_response = AsyncMock(return_value=sentinel)

    request = make_request(
        {"x-routstr-verify": "tlsn-proxy", "x-routstr-tlsn-session": "sess-err"}
    )
    result = await call_forward(request, provider)

    assert result is sentinel
    call = provider.forward_upstream_error_response.await_args
    assert call.kwargs["model_id"] == "gpt-test"
    mapped_response = call.args[2]
    assert mapped_response.status_code == 401
    assert mapped_response.content == error_body


@pytest.mark.asyncio
async def test_prepare_headers_strips_tlsn_control_headers() -> None:
    provider = make_provider()
    headers = provider.prepare_headers(
        {
            "x-routstr-verify": "tlsn-proxy",
            "x-routstr-tlsn-session": "sess-1",
            "content-type": "application/json",
        }
    )
    assert "x-routstr-verify" not in headers
    assert "x-routstr-tlsn-session" not in headers
    assert headers["Authorization"] == "Bearer upstream-secret-key"
