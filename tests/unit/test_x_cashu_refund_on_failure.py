import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from routstr.payment.cost_calculation import MaxCostData
from routstr.upstream.base import BaseUpstreamProvider


@pytest.fixture(params=["chat", "responses"])
def api(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def request_for(api: str) -> Request:
    path = "v1/chat/completions" if api == "chat" else "v1/responses"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/" + path,
            "headers": [],
            "query_string": b"",
        }
    )
    request.state.request_id = "refund-request"
    return request


async def paid_request(provider: BaseUpstreamProvider, api: str) -> Response:
    method = (
        provider.handle_x_cashu if api == "chat" else provider.handle_x_cashu_responses
    )
    request = request_for(api)
    return await method(
        request,
        "cashuApayment",
        request.url.path.lstrip("/"),
        1501,
        MagicMock(),
        request_body=b"{}",
    )


async def body(response: Response) -> bytes:
    if isinstance(response, StreamingResponse):
        return b"".join(
            [
                chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                async for chunk in response.body_iterator
            ]
        )
    return bytes(response.body)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "read", "prepare", "record", "non200"])
async def test_redeemed_payment_failure_refunds_once(api: str, failure: str) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    client = MagicMock()
    client.aclose = AsyncMock()
    response = httpx.Response(503 if failure == "non200" else 200, content=b"{}")
    if failure == "read":
        response.aread = AsyncMock(side_effect=httpx.ReadError("read failed"))  # type: ignore[method-assign]
    client.send = AsyncMock(
        side_effect=httpx.ConnectTimeout("timeout") if failure == "timeout" else None,
        return_value=response,
    )
    with (
        patch(
            "routstr.upstream.base.recieve_token",
            AsyncMock(return_value=(20, "sat", "mint")),
        ),
        patch(
            "routstr.upstream.base.store_cashu_transaction",
            AsyncMock(
                side_effect=RuntimeError("database down")
                if failure == "record"
                else None
            ),
        ),
        patch("routstr.upstream.base.build_x_cashu_client", return_value=client),
        patch.object(
            provider,
            "prepare_headers",
            side_effect=RuntimeError("prepare failed")
            if failure == "prepare"
            else None,
            return_value={},
        ),
        patch.object(
            provider, "send_refund", AsyncMock(return_value="cashuArefund")
        ) as refund,
    ):
        result = await paid_request(provider, api)

    assert result.status_code >= 400
    assert result.headers["X-Cashu"] == "cashuArefund"
    assert json.loads(await body(result))["error"]["refund_token"] == "cashuArefund"
    refund.assert_awaited_once_with(20, "sat", "mint", request_id="refund-request")
    if failure in ("timeout", "read", "non200"):
        client.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("unit", "amount", "expected"),
    [("sat", 20, 18), ("msat", 20000, 18499), ("sat", 1, 0)],
)
async def test_settlement_failure_serves_body_with_capped_charge(
    api: str,
    streaming: bool,
    unit: str,
    amount: int,
    expected: int,
) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    payload = (
        b'data: {"model":"m"}\n\ndata: [DONE]\n\n' if streaming else b'{"model":"m"}'
    )
    response = httpx.Response(200, content=payload)
    handler = (
        "handle_x_cashu_"
        + ("streaming" if streaming else "non_streaming")
        + ("_responses" if api == "responses" else "")
        + "_response"
    )
    completion = (
        provider.handle_x_cashu_chat_completion
        if api == "chat"
        else provider.handle_x_cashu_responses_completion
    )
    with (
        patch.object(
            provider, handler, AsyncMock(side_effect=RuntimeError("settlement failed"))
        ),
        patch.object(
            provider, "send_refund", AsyncMock(return_value="cashuArefund")
        ) as refund,
    ):
        result = await completion(
            response, amount, unit, 1501, "mint", request_id="refund-request"
        )
        assert await body(result) == payload
    assert result.status_code == 200
    if expected:
        refund.assert_awaited_once_with(
            expected, unit, "mint", request_id="refund-request"
        )
        assert result.headers["X-Cashu"] == "cashuArefund"
    else:
        refund.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_refund_failure_is_visible_without_retry(
    api: str, streaming: bool
) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    data = {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}
    payload = (
        ("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n")
        if streaming
        else json.dumps(data)
    )
    completion = (
        provider.handle_x_cashu_chat_completion
        if api == "chat"
        else provider.handle_x_cashu_responses_completion
    )
    with (
        patch.object(
            provider,
            "get_x_cashu_cost",
            AsyncMock(
                return_value=MaxCostData(
                    base_msats=0, input_msats=0, output_msats=0, total_msats=1000
                )
            ),
        ),
        patch.object(
            provider,
            "send_refund",
            AsyncMock(side_effect=RuntimeError("mint unavailable")),
        ) as refund,
        patch("routstr.upstream.base.logger.error") as log,
    ):
        result = await completion(
            httpx.Response(200, content=payload),
            20,
            "sat",
            1501,
            "mint",
            request_id="refund-request",
        )
    assert result.status_code == 200
    assert await body(result) == payload.encode()
    assert result.headers["X-Routstr-Refund-Error"] == "refund_failed"
    assert "X-Cashu" not in result.headers
    refund.assert_awaited_once()
    assert any(
        call.kwargs.get("extra", {}).get("request_id") == "refund-request"
        for call in log.call_args_list
    )


@pytest.mark.asyncio
async def test_created_refund_survives_storage_failure(api: str) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    client = MagicMock()
    client.aclose = AsyncMock()
    client.send = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
    with (
        patch(
            "routstr.upstream.base.recieve_token",
            AsyncMock(return_value=(20, "sat", "mint")),
        ),
        patch(
            "routstr.upstream.base.store_cashu_transaction",
            AsyncMock(side_effect=[True, RuntimeError("database down")]),
        ),
        patch(
            "routstr.upstream.base.send_token", AsyncMock(return_value="cashuArefund")
        ) as send,
        patch("routstr.upstream.base.token_mint_url", return_value="mint"),
        patch("routstr.upstream.base.build_x_cashu_client", return_value=client),
    ):
        result = await paid_request(provider, api)
    # The client holds a valid refund, so this reads as the upstream failure.
    assert result.status_code == 424
    assert result.headers["X-Cashu"] == "cashuArefund"
    error = json.loads(await body(result))["error"]
    assert error["code"] == "UPSTREAM_UNAVAILABLE"
    assert error["refund_token"] == "cashuArefund"
    send.assert_awaited_once_with(20, unit="sat", mint_url="mint")


@pytest.mark.asyncio
async def test_forwarding_cancellation_propagates(api: str) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    client = MagicMock()
    client.aclose = AsyncMock()
    client.send = AsyncMock(side_effect=asyncio.CancelledError())
    with (
        patch(
            "routstr.upstream.base.recieve_token",
            AsyncMock(return_value=(20, "sat", "mint")),
        ),
        patch("routstr.upstream.base.store_cashu_transaction", AsyncMock()),
        patch("routstr.upstream.base.build_x_cashu_client", return_value=client),
        patch.object(provider, "send_refund", AsyncMock()) as refund,
    ):
        with pytest.raises(asyncio.CancelledError):
            await paid_request(provider, api)
    refund.assert_not_awaited()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 503])
async def test_refund_failure_reaches_payment_boundary(api: str, status: int) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    client = MagicMock()
    client.aclose = AsyncMock()
    client.send = AsyncMock(return_value=httpx.Response(status, json={"model": "m"}))
    with (
        patch(
            "routstr.upstream.base.recieve_token",
            AsyncMock(return_value=(20, "sat", "mint")),
        ),
        patch("routstr.upstream.base.store_cashu_transaction", AsyncMock()),
        patch("routstr.upstream.base.build_x_cashu_client", return_value=client),
        patch.object(
            provider,
            "get_x_cashu_cost",
            AsyncMock(
                return_value=MaxCostData(
                    base_msats=0, input_msats=0, output_msats=0, total_msats=1000
                )
            ),
        ),
        patch.object(
            provider, "send_refund", AsyncMock(side_effect=RuntimeError("mint failed"))
        ) as refund,
    ):
        result = await paid_request(provider, api)
    if status == 200:
        assert result.status_code == 200
        assert json.loads(await body(result)) == {"model": "m"}
        assert result.headers["X-Routstr-Refund-Error"] == "refund_failed"
    else:
        assert result.status_code == 500
        assert json.loads(await body(result))["error"]["code"] == "refund_failed"
    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_non200_cleanup_failure_does_not_refund_twice(api: str) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    client = MagicMock()
    client.send = AsyncMock(
        return_value=httpx.Response(503, content=b"upstream failed")
    )
    with (
        patch(
            "routstr.upstream.base.recieve_token",
            AsyncMock(return_value=(20, "sat", "mint")),
        ),
        patch("routstr.upstream.base.store_cashu_transaction", AsyncMock()),
        patch("routstr.upstream.base.build_x_cashu_client", return_value=client),
        patch(
            "routstr.upstream.base.close_upstream_exchange",
            AsyncMock(side_effect=[RuntimeError("close failed"), None]),
        ),
        patch.object(
            provider, "send_refund", AsyncMock(return_value="cashuArefund")
        ) as refund,
    ):
        result = await paid_request(provider, api)
    assert result.headers["X-Cashu"] == "cashuArefund"
    refund.assert_awaited_once_with(20, "sat", "mint", request_id="refund-request")


@pytest.mark.asyncio
@pytest.mark.parametrize("after_refund", [False, True])
async def test_stream_settlement_exception_preserves_refund(
    api: str, after_refund: bool
) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    payload = b'data: {"model":"m","usage":{"input_tokens":1,"output_tokens":1}}\n\ndata: [DONE]\n\n'
    completion = (
        provider.handle_x_cashu_chat_completion
        if api == "chat"
        else provider.handle_x_cashu_responses_completion
    )
    with (
        patch.object(
            provider,
            "get_x_cashu_cost",
            AsyncMock(
                side_effect=None if after_refund else RuntimeError("cost failed"),
                return_value=MaxCostData(
                    base_msats=0, input_msats=0, output_msats=0, total_msats=1000
                ),
            ),
        ),
        patch.object(
            provider,
            "_stamp_streamed_provider",
            side_effect=RuntimeError("body processing failed"),
        ),
        patch.object(
            provider, "send_refund", AsyncMock(return_value="cashuArefund")
        ) as refund,
    ):
        result = await completion(
            httpx.Response(200, content=payload),
            20,
            "sat",
            1501,
            "mint",
            request_id="refund-request",
        )
    assert result.status_code == 200
    assert await body(result) == payload
    assert result.headers["X-Cashu"] == "cashuArefund"
    refund.assert_awaited_once_with(
        19 if after_refund else 18, "sat", "mint", request_id="refund-request"
    )


@pytest.mark.asyncio
async def test_settlement_cancellation_propagates(api: str) -> None:
    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    completion = (
        provider.handle_x_cashu_chat_completion
        if api == "chat"
        else provider.handle_x_cashu_responses_completion
    )
    with (
        patch.object(
            provider,
            "get_x_cashu_cost",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        patch.object(provider, "send_refund", AsyncMock()) as refund,
    ):
        with pytest.raises(asyncio.CancelledError):
            await completion(httpx.Response(200, json={"model": "m"}), 20, "sat", 1501)
    refund.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_failed_non_streaming_settlement_marks_what_the_fallback_keeps(
    api: str,
) -> None:
    from routstr.core.terminal_outcomes import TerminalOutcomeContext

    provider = BaseUpstreamProvider(base_url="http://upstream", api_key="test")
    mark_unknown = MagicMock()
    kwargs: dict[str, Any] = (
        {"terminal_outcome": TerminalOutcomeContext("request-1", "m")}
        if api == "chat"
        else {}
    )
    completion = (
        provider.handle_x_cashu_chat_completion
        if api == "chat"
        else provider.handle_x_cashu_responses_completion
    )
    with (
        patch.object(
            provider,
            "get_x_cashu_cost",
            AsyncMock(side_effect=RuntimeError("pricing unavailable")),
        ),
        patch.object(
            provider, "send_refund", AsyncMock(return_value="cashuArefund")
        ) as refund,
        patch("routstr.upstream.base.mark_terminal_outcome_unknown", mark_unknown),
    ):
        result = await completion(
            httpx.Response(200, content=b'{"model":"m"}'),
            20,
            "sat",
            1501,
            "mint",
            request_id="refund-request",
            **kwargs,
        )

    assert result.status_code == 200
    refund.assert_awaited_once_with(18, "sat", "mint", request_id="refund-request")
    mark_unknown.assert_called_once()
    assert mark_unknown.call_args.kwargs["revenue_msats"] == 2000
