"""Buffered image flow rejects before money and dispatches exactly once."""

import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request

from routstr.payment.cost_calculation import CostData
from routstr.payment.images import ImageQuote
from routstr.upstream import images


def request(headers=None, query=b""):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/images",
            "query_string": query,
            "headers": [
                (k.encode(), v.encode())
                for k, v in (
                    headers
                    or {
                        "content-type": "application/json",
                        "authorization": "Bearer sk-test",
                    }
                ).items()
            ],
        }
    )


def quote():
    return ImageQuote(
        "image-model",
        "provider",
        b'{"model":"image-model","prompt":"test"}',
        Decimal("0.01"),
        Decimal("1"),
        Decimal("0.00005"),
        200000,
    )


def cost():
    return CostData(
        base_msats=0,
        input_msats=0,
        output_msats=100001,
        total_msats=100001,
        total_usd=0.00500005,
    )


@pytest.fixture
def configured(monkeypatch):
    import routstr.proxy as proxy

    monkeypatch.setattr(images.settings, "image_generation_enabled", True)
    monkeypatch.setattr(images.settings, "image_max_request_usd", 1.0)
    model = SimpleNamespace(
        id="image-model",
        forwarded_model_id=None,
        enabled=True,
        api_capabilities={"images": {"fetched_at": int(time.time()), "endpoints": []}},
    )
    upstream = SimpleNamespace(
        provider_type="openrouter",
        base_url="https://openrouter.ai/api/v1",
        provider_fee=1.0,
        transform_model_name=lambda model_id: model_id,
        _apply_provider_field=Mock(),
    )
    monkeypatch.setattr(proxy, "get_candidates", lambda _: [(model, upstream)])
    monkeypatch.setattr(images, "quote_image_request", Mock(return_value=quote()))
    monkeypatch.setattr(images, "sats_usd_price", lambda: 0.00005)
    monkeypatch.setattr(images, "calculate_image_cost", Mock(return_value=cost()))
    monkeypatch.setattr(
        images,
        "generate_buffered_image",
        AsyncMock(
            return_value={"data": [{"b64_json": "aGVsbG8="}], "usage": {"cost": 0.005}}
        ),
    )
    return model, upstream


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,query",
    [
        ({"content-type": "multipart/form-data"}, b""),
        ({"content-type": "application/json", "ehbp-encapsulated-key": "opaque"}, b""),
        ({"content-type": "application/json", "x-routstr-model-path": "override"}, b""),
        ({"content-type": "application/json"}, b"provider=x"),
    ],
)
async def test_rejects_before_quote_or_payment(configured, headers, query):
    result = await images.forward_image_request(
        request(headers, query), AsyncMock(), b'{"model":"image-model"}'
    )
    assert result.status_code == 400
    images.quote_image_request.assert_not_called()
    images.generate_buffered_image.assert_not_called()


@pytest.mark.asyncio
async def test_stale_metadata_rejects_before_dispatch(configured):
    model, _ = configured
    model.api_capabilities["images"]["fetched_at"] = 1
    result = await images.forward_image_request(
        request(), AsyncMock(), b'{"model":"image-model"}'
    )
    assert result.status_code == 400
    images.generate_buffered_image.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_rejects(configured, monkeypatch):
    monkeypatch.setattr(images.settings, "image_generation_enabled", False)
    result = await images.forward_image_request(request(), AsyncMock(), b"{}")
    assert result.status_code == 503
    images.quote_image_request.assert_not_called()


@pytest.mark.asyncio
async def test_account_uses_snapshot_and_precomputed_cost(configured, monkeypatch):
    import routstr.proxy as proxy

    key = SimpleNamespace(balance=123456)
    session = AsyncMock()
    snapshot = SimpleNamespace(reserved_msats=200000)
    monkeypatch.setattr(proxy, "get_bearer_token_key", AsyncMock(return_value=key))
    monkeypatch.setattr(images, "pay_for_request", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        images, "adjust_payment_for_tokens", AsyncMock(return_value=cost())
    )
    monkeypatch.setattr(images, "revert_pay_for_request", AsyncMock())
    result = await images.forward_image_request(
        request(), session, b'{"model":"image-model"}'
    )
    assert result.status_code == 200
    images.generate_buffered_image.assert_awaited_once()
    assert (
        images.adjust_payment_for_tokens.call_args.kwargs[
            "precomputed_cost"
        ].total_msats
        == 100001
    )
    assert images.adjust_payment_for_tokens.call_args.args[-1] is snapshot
    images.revert_pay_for_request.assert_not_called()
    assert result.headers["x-routstr-cost-msats"] == "100001"


@pytest.mark.asyncio
async def test_account_timeout_releases_once_without_retry(configured, monkeypatch):
    import routstr.proxy as proxy

    key = SimpleNamespace(balance=1)
    session = AsyncMock()
    snapshot = SimpleNamespace(reserved_msats=200000)
    monkeypatch.setattr(proxy, "get_bearer_token_key", AsyncMock(return_value=key))
    monkeypatch.setattr(images, "pay_for_request", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(images, "revert_pay_for_request", AsyncMock())
    images.generate_buffered_image.side_effect = TimeoutError()
    result = await images.forward_image_request(
        request(), session, b'{"model":"image-model"}'
    )
    assert result.status_code == 502
    images.generate_buffered_image.assert_awaited_once()
    images.revert_pay_for_request.assert_awaited_once_with(
        key, session, 200000, snapshot
    )


@pytest.mark.asyncio
async def test_cashu_rejected_before_quote_or_redemption(configured):
    result = await images.forward_image_request(
        request({"content-type": "application/json", "x-cashu": "cashu-test"}),
        AsyncMock(),
        b'{"model":"image-model"}',
    )
    assert result.status_code == 400
    assert b"x_cashu_unsupported_endpoint" in result.body
    images.quote_image_request.assert_not_called()
    images.generate_buffered_image.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type,body,status",
    [
        ("application/json", b'{"data":[]}', 200),
        ("text/event-stream", b"data: preview", 200),
        ("application/json", b"{}", 502),
        ("application/json", b"[]", 200),
    ],
)
async def test_transport_one_post_and_safe_headers(
    monkeypatch, content_type, body, status
):
    calls = []

    async def handler(req):
        calls.append(req)
        return httpx.Response(
            status, headers={"content-type": content_type}, content=body
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        images.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    upstream = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1",
        prepare_headers=lambda headers: {
            **headers,
            "authorization": "Bearer provider-secret",
        },
    )
    if status == 200 and body == b'{"data":[]}':
        assert await images.generate_buffered_image(
            upstream=upstream, quote=quote()
        ) == {"data": []}
    else:
        with pytest.raises(images.ImageRequestError):
            await images.generate_buffered_image(upstream=upstream, quote=quote())
    assert len(calls) == 1
    assert calls[0].url.path == "/api/v1/images"
    assert calls[0].content == quote().body_json
    assert "x-cashu" not in calls[0].headers


@pytest.mark.asyncio
async def test_transport_bounds_decompressed_response(monkeypatch):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{}" * 20
        )
    )
    monkeypatch.setattr(
        images.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    monkeypatch.setattr(images.settings, "image_max_response_bytes", 10)
    upstream = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1", prepare_headers=lambda headers: headers
    )
    with pytest.raises(images.ImageRequestError, match="configured limit"):
        await images.generate_buffered_image(upstream=upstream, quote=quote())


@pytest.mark.asyncio
async def test_images_dispatch_bypasses_generic_retry_path(monkeypatch):
    import routstr.proxy as proxy

    dispatch = AsyncMock(
        return_value=images.Response("{}", media_type="application/json")
    )
    monkeypatch.setattr(images, "forward_image_request", dispatch)
    req = request()
    session = AsyncMock()
    result = await proxy._proxy(req, "v1/images", session, b"{}")
    assert result.status_code == 200
    dispatch.assert_awaited_once_with(req, session, b"{}")


@pytest.mark.asyncio
async def test_quote_uses_upstream_model_not_client_alias(configured, monkeypatch):
    import routstr.proxy as proxy

    model, upstream = configured
    model.id = "upstream/image-model"
    model.forwarded_model_id = "client-alias"
    upstream.transform_model_name = Mock(return_value="wire/image-model")
    monkeypatch.setattr(
        proxy,
        "get_bearer_token_key",
        AsyncMock(side_effect=images.HTTPException(401, "invalid key")),
    )
    with pytest.raises(images.HTTPException):
        await images.forward_image_request(
            request(), AsyncMock(), b'{"model":"client-alias","prompt":"test"}'
        )
    upstream.transform_model_name.assert_called_once_with("upstream/image-model")
    assert (
        images.quote_image_request.call_args.kwargs["upstream_model_id"]
        == "wire/image-model"
    )
    images.generate_buffered_image.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outputs", [["image"], ["image", "audio"]])
@pytest.mark.parametrize(
    "path", ["v1/chat/completions", "v1/responses", "v1/messages", "v1/completions"]
)
async def test_image_catalogue_outage_never_routes_to_chat(monkeypatch, outputs, path):
    import routstr.proxy as proxy

    model = SimpleNamespace(
        architecture=SimpleNamespace(output_modalities=outputs), api_capabilities={}
    )
    upstream = SimpleNamespace(forward_request=AsyncMock())
    monkeypatch.setattr(proxy, "get_candidates", lambda _: [(model, upstream)])
    auth = AsyncMock()
    reserve = AsyncMock()
    monkeypatch.setattr(proxy, "get_bearer_token_key", auth)
    monkeypatch.setattr(proxy, "pay_for_request", reserve)
    result = await proxy._proxy(
        request(), path, AsyncMock(), b'{"model":"openai/gpt-image-2.5-flare"}'
    )
    assert result.status_code == 400
    assert b"requires the Images API" in result.body
    auth.assert_not_called()
    reserve.assert_not_called()
    upstream.forward_request.assert_not_called()
