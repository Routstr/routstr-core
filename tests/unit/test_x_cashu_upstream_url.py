"""X-Cashu requests reach the same upstream URL and framing as API-key requests."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.responses import Response

from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.ollama import OllamaUpstreamProvider

MODEL = Model(
    id="nimble:9b",
    name="nimble:9b",
    created=0,
    description="",
    context_length=32_000,
    architecture=Architecture(
        modality="text->decisions",
        input_modalities=["text"],
        output_modalities=["decisions"],
        tokenizer="Other",
        instruct_type=None,
    ),
    pricing=Pricing(prompt=0.021 / 1_000_000, completion=0.0),
)

BODY = {"model": "nimble:9b", "state": "s", "questions": {"q": {"type": "noul"}}}


async def _x_cashu_url(provider: BaseUpstreamProvider, path: str) -> str:
    request = MagicMock()
    request.method = "POST"
    request.query_params = {}
    request.state.request_id = "req-1"
    request.body = AsyncMock(return_value=json.dumps(BODY).encode())
    upstream = httpx.Response(
        200,
        content=b'{"answers": {}}',
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "http://upstream"),
    )
    send = AsyncMock(return_value=upstream)
    settle = AsyncMock(return_value=Response(content=b"{}", status_code=200))

    with (
        patch("httpx.AsyncClient.send", send),
        patch.object(provider, "handle_x_cashu_chat_completion", settle),
    ):
        await provider.forward_x_cashu_request(
            request, path, {}, 10, "sat", 1000, MODEL
        )
    return str(send.call_args.args[0].url)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1/systemone", "v1/chat/completions"])
async def test_ollama_x_cashu_keeps_v1_prefix(path: str) -> None:
    provider = OllamaUpstreamProvider(base_url="http://ollama:11434", provider_fee=1.0)

    url = await _x_cashu_url(provider, path)

    assert url == f"http://ollama:11434/{path}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1/systemone", "v1/chat/completions"])
async def test_x_cashu_url_matches_api_key_url(path: str) -> None:
    for provider in (
        BaseUpstreamProvider(base_url="http://upstream/v1", api_key="k"),
        OllamaUpstreamProvider(base_url="http://ollama:11434"),
    ):
        expected = provider.build_request_url(
            provider.normalize_request_path(path, MODEL), MODEL
        )

        assert await _x_cashu_url(provider, path) == expected


@pytest.mark.asyncio
async def test_x_cashu_response_length_matches_rewritten_body() -> None:
    """Upstream Content-Length must not survive the cost-injected body."""
    from routstr.payment.cost_calculation import CostData

    raw = json.dumps(
        {
            "model": "nimble:9b",
            "answers": {"q": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 939, "output_tokens": 0},
        }
    )
    upstream = httpx.Response(
        200,
        content=raw.encode(),
        headers={"content-type": "application/json", "content-length": str(len(raw))},
        request=httpx.Request("POST", "http://ollama:11434/v1/systemone"),
    )
    provider = OllamaUpstreamProvider(base_url="http://ollama:11434", provider_fee=1.0)
    cost = CostData(
        base_msats=0,
        input_msats=23,
        output_msats=0,
        total_msats=23,
        total_usd=0.0000198,
        input_tokens=939,
    )

    with (
        patch.object(provider, "get_x_cashu_cost", AsyncMock(return_value=cost)),
        patch.object(provider, "send_refund", AsyncMock(return_value="cashuBrefund")),
    ):
        response = await provider.handle_x_cashu_non_streaming_response(
            raw, upstream, 99, "sat", 1000, model_obj=MODEL
        )

    assert len(response.body) > len(raw)
    assert response.headers["content-length"] == str(len(response.body))
    assert response.headers["X-Cashu"] == "cashuBrefund"


@pytest.mark.asyncio
async def test_x_cashu_responses_url_matches_api_key_url() -> None:
    """The Responses API has its own X-Cashu forwarder; it needs the same URL."""
    for provider in (
        BaseUpstreamProvider(base_url="http://upstream/v1", api_key="k"),
        OllamaUpstreamProvider(base_url="http://ollama:11434"),
    ):
        request = MagicMock()
        request.method = "POST"
        request.query_params = {}
        request.state.request_id = "req-1"
        upstream = httpx.Response(
            200,
            content=b'{"output": []}',
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "http://upstream"),
        )
        send = AsyncMock(return_value=upstream)
        settle = AsyncMock(return_value=Response(content=b"{}", status_code=200))
        body = json.dumps({"model": "nimble:9b", "input": "hi"}).encode()

        with (
            patch("httpx.AsyncClient.send", send),
            patch.object(provider, "handle_x_cashu_responses_completion", settle),
        ):
            await provider.forward_x_cashu_responses_request(
                request, "v1/responses", {}, 10, "sat", 1000, MODEL, request_body=body
            )

        expected = provider.build_request_url(
            provider.normalize_request_path("v1/responses", MODEL), MODEL
        )
        assert str(send.call_args.args[0].url) == expected
