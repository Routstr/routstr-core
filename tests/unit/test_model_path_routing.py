"""Model-path routing and fail-closed behavior."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr import proxy as proxy_module
from routstr.auth import ReservationSnapshot
from routstr.core.db import ApiKey
from routstr.core.error_scope import (
    ERROR_SCOPE_HEADER,
    ERROR_SCOPE_NODE,
    ERROR_SCOPE_UPSTREAM,
    UPSTREAM_UNAVAILABLE,
)
from routstr.upstream.model_paths import decode_model_path, encode_model_path

from .proxy_test_utils import mock_request_stream, patch_proxy_session

MODEL_ID = "test-model"


def _make_upstream(db_id: int, status_code: int = 200) -> MagicMock:
    upstream = MagicMock()
    upstream.db_id = db_id
    upstream.base_url = "http://localhost"
    upstream.provider_type = f"provider-{db_id}"
    upstream.supports_ehbp = False
    upstream.prepare_headers = MagicMock(side_effect=lambda h: h)
    upstream.on_upstream_error_redirect = AsyncMock()
    upstream.forward_request = AsyncMock(
        return_value=MagicMock(status_code=status_code, body=b"{}")
    )
    return upstream


def _make_request(headers: dict[str, str], body: bytes) -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    request.headers = headers
    mock_request_stream(request, body)
    request.state = MagicMock()
    request.state.request_id = "req-model-path"
    return request


async def _run_proxy(
    request: MagicMock,
    candidates: list[tuple[Any, Any]],
    path: str = "v1/chat/completions",
    session: Any = None,
) -> Any:
    key = ApiKey(hashed_key="mpkey", balance=10_000)
    reservation = ReservationSnapshot(
        release_id="model-path-release",
        key_hash=key.hashed_key,
        billing_key_hash=key.hashed_key,
        reserved_msats=1_000,
    )
    with (
        patch.object(proxy_module, "get_candidates", return_value=candidates),
        patch.object(
            proxy_module, "get_max_cost_for_model", AsyncMock(return_value=1_000)
        ),
        patch.object(
            proxy_module,
            "calculate_discounted_max_cost",
            AsyncMock(return_value=1_000),
        ),
        patch.object(proxy_module, "check_token_balance", MagicMock()),
        patch.object(proxy_module, "get_bearer_token_key", AsyncMock(return_value=key)),
        patch.object(
            proxy_module, "pay_for_request", AsyncMock(return_value=reservation)
        ),
        patch.object(proxy_module, "revert_pay_for_request", AsyncMock()),
        patch_proxy_session(session if session is not None else MagicMock()),
    ):
        return await proxy_module.proxy(request, path)


def test_decode_model_path_round_trips_encode() -> None:
    selector = decode_model_path(
        encode_model_path("https://openrouter.ai/api/v1", MODEL_ID, "deepinfra/fp8")
    )
    assert selector is not None
    assert selector.base_url == "https://openrouter.ai/api/v1"
    assert selector.provider_id is None
    assert selector.model_id == MODEL_ID
    assert selector.endpoint_tag == "deepinfra/fp8"


def test_encoded_path_carries_no_provider_id() -> None:
    assert "provider-id" not in encode_model_path("http://localhost", MODEL_ID)


def test_decode_model_path_still_accepts_a_legacy_provider_id() -> None:
    selector = decode_model_path(
        "url=http%3A%2F%2Flocalhost&provider-id=7&model-id=test-model"
    )
    assert selector is not None
    assert selector.provider_id == 7
    assert selector.base_url == "http://localhost"
    assert selector.model_id == MODEL_ID


def test_decode_model_path_without_endpoint_has_no_tag() -> None:
    selector = decode_model_path(encode_model_path("http://localhost", MODEL_ID))
    assert selector is not None
    assert selector.endpoint_tag is None


@pytest.mark.parametrize(
    "path",
    [
        "",
        "url=http://localhost&provider-id=abc&model-id=test-model",
        "url=http://localhost&provider-id=0&model-id=test-model",
        "provider-id=1&model-id=test-model",
        "url=http://localhost&provider-id=1",
        "model-id=test-model",
        "url=http://localhost",
    ],
)
def test_decode_model_path_rejects_malformed_selectors(path: str) -> None:
    assert decode_model_path(path) is None


@pytest.mark.asyncio
async def test_model_path_routes_to_the_cheapest_provider_sharing_the_url() -> None:
    # get_candidates ranks by cost, so the first match for a provider-less
    # selector is the cheapest provider configured against that URL.
    cheapest, pricier = _make_upstream(1), _make_upstream(2)
    request = _make_request(
        {
            "authorization": "Bearer sk-mpkey",
            "x-routstr-model-path": encode_model_path("http://localhost", MODEL_ID),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )

    await _run_proxy(request, [(MagicMock(), cheapest), (MagicMock(), pricier)])

    cheapest.forward_request.assert_awaited_once()
    pricier.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_cheapest_provider_failure_does_not_fall_back_to_the_pricier_one() -> (
    None
):
    cheapest, pricier = _make_upstream(1, status_code=503), _make_upstream(2)
    request = _make_request(
        {
            "authorization": "Bearer sk-mpkey",
            "x-routstr-model-path": encode_model_path("http://localhost", MODEL_ID),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )

    response = await _run_proxy(
        request, [(MagicMock(), cheapest), (MagicMock(), pricier)]
    )

    assert response.status_code == 503
    cheapest.forward_request.assert_awaited_once()
    pricier.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_provider_id_still_pins_that_exact_provider() -> None:
    cheapest, pinned = _make_upstream(1), _make_upstream(2)
    request = _make_request(
        {
            "authorization": "Bearer sk-mpkey",
            "x-routstr-model-path": (
                f"url=http%3A%2F%2Flocalhost&provider-id=2&model-id={MODEL_ID}"
            ),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )

    await _run_proxy(request, [(MagicMock(), cheapest), (MagicMock(), pinned)])

    pinned.forward_request.assert_awaited_once()
    cheapest.forward_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 429, 500, 502, 503])
async def test_model_path_failure_is_returned_without_falling_back(
    status_code: int,
) -> None:
    selected = _make_upstream(1, status_code=status_code)
    fallback = _make_upstream(2)
    request = _make_request(
        {
            "authorization": "Bearer sk-mpkey",
            "x-routstr-model-path": encode_model_path("http://localhost", MODEL_ID),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )

    response = await _run_proxy(
        request, [(MagicMock(), selected), (MagicMock(), fallback)]
    )

    assert response.status_code == status_code
    selected.forward_request.assert_awaited_once()
    fallback.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_legacy_provider_in_model_path_is_rejected() -> None:
    upstream = _make_upstream(1)
    request = _make_request(
        {
            "authorization": "Bearer sk-mpkey",
            "x-routstr-model-path": (
                f"url=http%3A%2F%2Flocalhost&provider-id=99&model-id={MODEL_ID}"
            ),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )

    response = await _run_proxy(request, [(MagicMock(), upstream)])

    assert response.status_code == 404
    assert json.loads(bytes(response.body))["error"]["type"] == "invalid_model_path"
    upstream.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_path_disagreeing_with_the_body_model_is_rejected() -> None:
    upstream = _make_upstream(1)
    request = _make_request(
        {
            "authorization": "Bearer sk-mpkey",
            "x-routstr-model-path": encode_model_path(
                "http://localhost", "other-model"
            ),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )

    response = await _run_proxy(request, [(MagicMock(), upstream)])

    assert response.status_code == 400
    upstream.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_model_path_header_is_rejected() -> None:
    upstream = _make_upstream(1)
    request = _make_request(
        {
            "authorization": "Bearer sk-mpkey",
            "x-routstr-model-path": "not-a-model-path",
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )

    response = await _run_proxy(request, [(MagicMock(), upstream)])

    assert response.status_code == 400
    upstream.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_tag_pins_the_upstream_subprovider() -> None:
    upstream = _make_upstream(1)
    upstream.base_url = "https://openrouter.ai/api/v1"
    request = _make_request(
        {
            "authorization": "Bearer sk-mpkey",
            "x-routstr-model-path": encode_model_path(
                "https://openrouter.ai/api/v1", MODEL_ID, "deepinfra/fp8"
            ),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )

    await _run_proxy(request, [(MagicMock(), upstream)])

    forwarded_body = upstream.forward_request.await_args.args[3]
    assert json.loads(forwarded_body)["provider"] == {
        "order": ["deepinfra/fp8"],
        "allow_fallbacks": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    ["", " ", "url=http://localhost&provider-id=1&model-id=test-model&provider-id=2"],
)
async def test_ambiguous_headers_do_not_route(raw: str) -> None:
    upstream = _make_upstream(1)
    request = _make_request(
        {"authorization": "Bearer key", "x-routstr-model-path": raw},
        json.dumps({"model": MODEL_ID}).encode(),
    )
    response = await _run_proxy(request, [(MagicMock(), upstream)])
    assert response.status_code == 400
    upstream.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_selector_url_must_match_configured_provider() -> None:
    upstream = _make_upstream(1)
    request = _make_request(
        {
            "authorization": "Bearer key",
            "x-routstr-model-path": encode_model_path(
                "http://169.254.169.254", MODEL_ID
            ),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )
    response = await _run_proxy(request, [(MagicMock(), upstream)])
    assert response.status_code == 404
    upstream.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_pin_cannot_be_stripped_on_retry() -> None:
    upstream = _make_upstream(1, 400)
    upstream.base_url = "https://openrouter.ai/api/v1"
    upstream.forward_request.return_value.body = (
        b'{"error":{"message":"provider is not supported"}}'
    )
    request = _make_request(
        {
            "authorization": "Bearer key",
            "x-routstr-model-path": encode_model_path(
                upstream.base_url, MODEL_ID, "deepinfra/fp8"
            ),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )
    response = await _run_proxy(request, [(MagicMock(), upstream)])
    assert response.status_code == 400
    upstream.forward_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,handler",
    [
        ("v1/chat/completions", "handle_x_cashu"),
        ("v1/responses", "handle_x_cashu_responses"),
    ],
)
async def test_cashu_receives_endpoint_pinned_body(path: str, handler: str) -> None:
    upstream = _make_upstream(1)
    upstream.base_url = "https://openrouter.ai/api/v1"
    captured: dict[str, Any] = {}

    async def handle(request: Any, *args: Any, **kwargs: Any) -> Any:
        captured.update(json.loads(kwargs.get("request_body") or await request.body()))
        return MagicMock(status_code=200)

    setattr(upstream, handler, AsyncMock(side_effect=handle))
    request = _make_request(
        {
            "x-cashu": "test-token",
            "x-routstr-model-path": encode_model_path(
                upstream.base_url, MODEL_ID, "deepinfra/fp8"
            ),
        },
        json.dumps(
            {
                "model": MODEL_ID,
                "provider": {
                    "order": ["other"],
                    "allow_fallbacks": True,
                    "data_collection": "deny",
                },
            }
        ).encode(),
    )
    await _run_proxy(request, [(MagicMock(), upstream)], path)
    assert captured["provider"] == {
        "order": ["deepinfra/fp8"],
        "allow_fallbacks": False,
        "data_collection": "deny",
    }


def test_model_path_header_is_not_forwarded() -> None:
    from routstr.upstream.base import BaseUpstreamProvider

    upstream = BaseUpstreamProvider("http://localhost", "upstream-key")
    headers = upstream.prepare_headers({"x-routstr-model-path": "private-routing-data"})
    assert "x-routstr-model-path" not in headers


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1/chat/completions", "v1/responses"])
@pytest.mark.parametrize(
    "status_code,client_status",
    [(200, 200), (429, 429), (502, 424)],
)
async def test_cashu_pin_reaches_http_transport(
    path: str, status_code: int, client_status: int
) -> None:
    import httpx
    from fastapi.responses import Response

    from routstr.upstream.openrouter import OpenRouterUpstreamProvider

    upstream = OpenRouterUpstreamProvider(api_key="upstream-key")
    upstream.db_id = 1
    fallback = _make_upstream(2)
    fallback.handle_x_cashu = AsyncMock()
    fallback.handle_x_cashu_responses = AsyncMock()
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status_code, json={"error": "test"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    model = MagicMock(id=MODEL_ID, forwarded_model_id=None, canonical_slug=None)
    request = _make_request(
        {
            "x-cashu": "test-token",
            "x-routstr-model-path": encode_model_path(
                upstream.base_url, MODEL_ID, "deepinfra/fp8"
            ),
        },
        json.dumps({"model": MODEL_ID, "provider": {"allow_fallbacks": True}}).encode(),
    )
    request.query_params = {}
    with (
        patch("routstr.upstream.base.httpx.AsyncClient", return_value=client),
        patch(
            "routstr.upstream.base.recieve_token",
            AsyncMock(return_value=(1000, "msat", "https://mint.test")),
        ) as redeem,
        patch("routstr.upstream.base.store_cashu_transaction", AsyncMock()),
        patch.object(upstream, "send_refund", AsyncMock(return_value="refund")),
        patch.object(
            upstream,
            "handle_x_cashu_chat_completion",
            AsyncMock(return_value=Response(status_code=200)),
        ),
        patch.object(
            upstream,
            "handle_x_cashu_responses_completion",
            AsyncMock(return_value=Response(status_code=200)),
        ),
    ):
        response = await _run_proxy(
            request, [(model, upstream), (model, fallback)], path
        )
    assert response.status_code == client_status
    if client_status == 424:
        assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
        body = json.loads(bytes(response.body))
        assert body["error"]["code"] == UPSTREAM_UNAVAILABLE
    redeem.assert_awaited_once()
    assert len(sent) == 1
    assert sent[0].url.host == "openrouter.ai"
    assert json.loads(sent[0].content)["provider"] == {
        "order": ["deepinfra/fp8"],
        "allow_fallbacks": False,
    }
    assert "x-routstr-model-path" not in sent[0].headers
    fallback.handle_x_cashu.assert_not_awaited()
    fallback.handle_x_cashu_responses.assert_not_awaited()


@pytest.mark.asyncio
async def test_unpinned_requests_still_fall_back() -> None:
    first, fallback = _make_upstream(1, 502), _make_upstream(2)
    request = _make_request(
        {"authorization": "Bearer key"}, json.dumps({"model": MODEL_ID}).encode()
    )
    response = await _run_proxy(
        request, [(MagicMock(), first), (MagicMock(), fallback)]
    )
    assert response.status_code == 200
    first.forward_request.assert_awaited_once()
    fallback.forward_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_pinned_exception_does_not_fall_back() -> None:
    from routstr.core.exceptions import UpstreamError

    first, fallback = _make_upstream(1), _make_upstream(2)
    first.forward_request.side_effect = UpstreamError("unavailable", status_code=503)
    request = _make_request(
        {
            "authorization": "Bearer key",
            "x-routstr-model-path": encode_model_path(first.base_url, MODEL_ID),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )
    response = await _run_proxy(
        request, [(MagicMock(), first), (MagicMock(), fallback)]
    )
    # Pinned: no fallback.
    assert response.status_code == 424
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    assert json.loads(bytes(response.body))["error"]["code"] == UPSTREAM_UNAVAILABLE
    first.forward_request.assert_awaited_once()
    fallback.forward_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,is_ehbp", [("v1/messages", False), ("v1/chat/completions", True)]
)
async def test_unsupported_endpoint_pins_fail_before_payment(
    path: str, is_ehbp: bool
) -> None:
    upstream = _make_upstream(1)
    upstream.base_url = "https://openrouter.ai/api/v1"
    headers = {
        "x-cashu": "test-token",
        "x-routstr-model-path": encode_model_path(
            upstream.base_url, MODEL_ID, "deepinfra/fp8"
        ),
    }
    if is_ehbp:
        headers.update({"ehbp-encapsulated-key": "sealed", "x-routstr-model": MODEL_ID})
    request = _make_request(headers, json.dumps({"model": MODEL_ID}).encode())
    with (
        patch.object(proxy_module, "check_token_balance") as payment,
        patch.object(
            proxy_module, "get_candidates", return_value=[(MagicMock(), upstream)]
        ),
        patch_proxy_session(MagicMock()),
    ):
        response = await proxy_module.proxy(request, path)
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["type"] == "unsupported_request"
    payment.assert_not_called()
    upstream.forward_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cashu", [False, True])
async def test_ehbp_pin_does_not_fall_back(cashu: bool) -> None:
    from routstr.core.exceptions import UpstreamError

    selected, fallback = _make_upstream(1), _make_upstream(2)
    selected.supports_ehbp = fallback.supports_ehbp = True
    headers = {
        "ehbp-encapsulated-key": "sealed",
        "x-routstr-model": MODEL_ID,
        "x-routstr-model-path": encode_model_path(selected.base_url, MODEL_ID),
    }
    headers.update({"x-cashu": "token"} if cashu else {"authorization": "Bearer key"})
    request = _make_request(headers, b"encrypted-body")
    handler = "forward_ehbp_x_cashu_request" if cashu else "forward_ehbp_request"
    with patch.object(
        proxy_module,
        handler,
        AsyncMock(side_effect=UpstreamError("unavailable", status_code=503)),
    ) as forward:
        response = await _run_proxy(
            request, [(MagicMock(), selected), (MagicMock(), fallback)]
        )
    assert response.status_code == 424
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    forward.assert_awaited_once()
    assert forward.await_args is not None
    assert forward.await_args.kwargs["upstream"] is selected


@pytest.mark.asyncio
async def test_duplicate_header_fields_are_rejected() -> None:
    from starlette.datastructures import Headers

    selected = _make_upstream(1)
    route = encode_model_path(selected.base_url, MODEL_ID).encode()
    request = _make_request({}, json.dumps({"model": MODEL_ID}).encode())
    request.headers = Headers(
        raw=[
            (b"authorization", b"Bearer key"),
            (b"x-routstr-model-path", route),
            (b"x-routstr-model-path", route),
        ]
    )
    response = await _run_proxy(request, [(MagicMock(), selected)])
    assert response.status_code == 400
    selected.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_attestation_does_not_ignore_model_path() -> None:
    request = _make_request(
        {"x-routstr-model-path": encode_model_path("http://localhost", MODEL_ID)},
        b"",
    )
    request.method = "GET"
    with patch.object(proxy_module, "_select_unauthenticated_get_upstreams") as select:
        response = await _run_proxy(request, [], "attestation")
    assert response.status_code == 400
    select.assert_not_called()


@pytest.mark.asyncio
async def test_model_fallback_list_is_rejected_when_pinned() -> None:
    selected = _make_upstream(1)
    request = _make_request(
        {
            "authorization": "Bearer key",
            "x-routstr-model-path": encode_model_path(selected.base_url, MODEL_ID),
        },
        json.dumps({"model": MODEL_ID, "models": ["other-model"]}).encode(),
    )
    response = await _run_proxy(request, [(MagicMock(), selected)])
    assert response.status_code == 400
    selected.forward_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [None, "deepinfra/fp8"])
@pytest.mark.parametrize("path", ["v1/chat/completions", "v1/responses"])
@pytest.mark.parametrize("final_status", [200, 429])
async def test_pinned_recovery_stays_on_selected_provider(
    endpoint: str | None, path: str, final_status: int
) -> None:
    selected, fallback = _make_upstream(1), _make_upstream(2)
    selected.base_url = "https://openrouter.ai/api/v1"
    handler = (
        "forward_responses_request" if path == "v1/responses" else "forward_request"
    )
    forward = AsyncMock(
        side_effect=[
            MagicMock(
                status_code=400,
                body=b'{"error":{"message":"temperature is deprecated"}}',
            ),
            MagicMock(status_code=final_status, body=b"{}"),
        ]
    )
    setattr(selected, handler, forward)
    setattr(fallback, handler, AsyncMock())
    request = _make_request(
        {
            "authorization": "Bearer key",
            "x-routstr-model-path": encode_model_path(
                selected.base_url, MODEL_ID, endpoint
            ),
        },
        json.dumps(
            {
                "model": MODEL_ID,
                "temperature": 0.7,
                "provider": {"data_collection": "deny"},
            }
        ).encode(),
    )

    response = await _run_proxy(
        request, [(MagicMock(), selected), (MagicMock(), fallback)], path
    )

    assert response.status_code == final_status
    assert forward.await_count == 2
    before, after = [json.loads(call.args[3]) for call in forward.await_args_list]
    assert "temperature" in before
    assert "temperature" not in after
    assert after["model"] == before["model"] == MODEL_ID
    assert after["provider"] == before["provider"]
    if endpoint:
        assert after["provider"]["order"] == [endpoint]
        assert after["provider"]["allow_fallbacks"] is False
    getattr(fallback, handler).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["model", "provider"])
@pytest.mark.parametrize("endpoint", [None, "deepinfra/fp8"])
async def test_pinned_recovery_preserves_routing_fields(
    field: str, endpoint: str | None
) -> None:
    selected, fallback = _make_upstream(1, 400), _make_upstream(2)
    selected.base_url = "https://openrouter.ai/api/v1"
    selected.forward_request.return_value.body = json.dumps(
        {"error": {"message": f"{field} is not supported"}}
    ).encode()
    request = _make_request(
        {
            "authorization": "Bearer key",
            "x-routstr-model-path": encode_model_path(
                selected.base_url, MODEL_ID, endpoint
            ),
        },
        json.dumps(
            {"model": MODEL_ID, "provider": {"data_collection": "deny"}}
        ).encode(),
    )

    response = await _run_proxy(
        request, [(MagicMock(), selected), (MagicMock(), fallback)]
    )

    assert response.status_code == 400
    selected.forward_request.assert_awaited_once()
    fallback.forward_request.assert_not_awaited()


_OPENAI_MAX_TOKENS_ERROR = json.dumps(
    {
        "error": {
            "message": "Unsupported parameter: 'max_tokens' is not supported "
            "with this model. Use 'max_completion_tokens' instead.",
            "type": "invalid_request_error",
            "param": "max_tokens",
            "code": "unsupported_parameter",
        }
    }
).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True])
async def test_rejected_max_tokens_is_renamed_and_retried_on_same_upstream(
    pinned: bool,
) -> None:
    selected, fallback = _make_upstream(1), _make_upstream(2)
    selected.forward_request = AsyncMock(
        side_effect=[
            MagicMock(status_code=400, body=_OPENAI_MAX_TOKENS_ERROR),
            MagicMock(status_code=200, body=b"{}"),
        ]
    )
    headers = {"authorization": "Bearer key"}
    if pinned:
        headers["x-routstr-model-path"] = encode_model_path(selected.base_url, MODEL_ID)
    request = _make_request(
        headers,
        json.dumps(
            {"model": MODEL_ID, "max_tokens": 300, "messages": [], "stream": True}
        ).encode(),
    )

    response = await _run_proxy(
        request, [(MagicMock(), selected), (MagicMock(), fallback)]
    )

    assert response.status_code == 200
    assert selected.forward_request.await_count == 2
    before, after = [
        json.loads(call.args[3]) for call in selected.forward_request.await_args_list
    ]
    assert before["max_tokens"] == 300 and "max_completion_tokens" not in before
    assert after["max_completion_tokens"] == 300 and "max_tokens" not in after
    assert {k: v for k, v in after.items() if k != "max_completion_tokens"} == {
        k: v for k, v in before.items() if k != "max_tokens"
    }
    fallback.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_that_changes_spend_bound_is_not_retried() -> None:
    selected = _make_upstream(1, 400)
    selected.forward_request.return_value.body = json.dumps(
        {"error": {"message": "'max_tokens' is not supported. Use 'n' instead."}}
    ).encode()
    request = _make_request(
        {"authorization": "Bearer key"},
        json.dumps({"model": MODEL_ID, "max_tokens": 300}).encode(),
    )

    response = await _run_proxy(request, [(MagicMock(), selected)])

    assert response.status_code == 400
    selected.forward_request.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Upstream 5xx -> 424 + UPSTREAM_UNAVAILABLE + scope header; node faults stay 500.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_upstream_424_fails_over_to_a_healthy_provider() -> None:
    """An upstream-attributed 424 is still retryable: the caller only ever
    sees the healthy provider's 200."""
    from routstr.core.exceptions import UpstreamError

    first, healthy = _make_upstream(1), _make_upstream(2)
    first.forward_request.side_effect = UpstreamError("bad gateway", status_code=502)
    request = _make_request(
        {"authorization": "Bearer key"}, json.dumps({"model": MODEL_ID}).encode()
    )

    response = await _run_proxy(request, [(MagicMock(), first), (MagicMock(), healthy)])

    assert response.status_code == 200
    first.forward_request.assert_awaited_once()
    healthy.forward_request.assert_awaited_once()
    # The caller never sees the upstream error body or any scope header.
    assert ERROR_SCOPE_HEADER not in response.headers


@pytest.mark.asyncio
async def test_last_candidate_upstream_failure_reports_424() -> None:
    """Every candidate failed on the provider hop: 424 + upstream scope, with
    the provider's own status preserved for operators."""
    from routstr.core.exceptions import UpstreamError

    only = _make_upstream(1)
    only.forward_request.side_effect = UpstreamError("bad gateway", status_code=502)
    request = _make_request(
        {"authorization": "Bearer key"}, json.dumps({"model": MODEL_ID}).encode()
    )

    response = await _run_proxy(request, [(MagicMock(), only)])

    assert response.status_code == 424
    assert response.headers[ERROR_SCOPE_HEADER] == ERROR_SCOPE_UPSTREAM
    body = json.loads(bytes(response.body))
    assert body["error"]["type"] == "upstream_error"
    assert body["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert body["error"]["details"]["upstream_status"] == 502


@pytest.mark.asyncio
async def test_node_fault_stays_500_without_scope_header() -> None:
    """A genuine node fault keeps its 500 and carries no scope header, so a
    client can still tell this node is the broken one."""
    from routstr.core.exceptions import UpstreamError

    only = _make_upstream(1)
    only.forward_request.side_effect = UpstreamError(
        "An unexpected server error occurred",
        status_code=500,
        scope=ERROR_SCOPE_NODE,
    )
    request = _make_request(
        {"authorization": "Bearer key"}, json.dumps({"model": MODEL_ID}).encode()
    )

    response = await _run_proxy(request, [(MagicMock(), only)])

    assert response.status_code == 500
    assert ERROR_SCOPE_HEADER not in response.headers
    body = json.loads(bytes(response.body))
    assert body["error"]["code"] != UPSTREAM_UNAVAILABLE


_OPENROUTER = "https://openrouter.ai/api/v1"
_SATS_USD = 0.001
_ENDPOINT_PRICING = {"prompt": 2e-6, "completion": 4e-6}


def _priced_model(
    prompt: float = 1e-6, completion: float = 2e-6, cache_read: float = 0.0
) -> Any:
    from routstr.payment.models import (
        Architecture,
        Model,
        Pricing,
        _calculate_usd_max_costs,
        _update_model_sats_pricing,
    )

    model = Model(
        id=MODEL_ID,
        name=MODEL_ID,
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="unknown",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=prompt, completion=completion, input_cache_read=cache_read
        ),
    )
    (
        model.pricing.max_prompt_cost,
        model.pricing.max_completion_cost,
        model.pricing.max_cost,
    ) = _calculate_usd_max_costs(model)
    return _update_model_sats_pricing(model, _SATS_USD)


def _endpoint_row(
    model_id: str = MODEL_ID,
    endpoint_tag: str = "deepinfra/fp8",
    pricing: dict[str, float] | None = None,
    **limits: int,
) -> Any:
    from routstr.core.db import ModelPathRow

    return ModelPathRow(
        model_id=model_id,
        path=encode_model_path(_OPENROUTER, model_id, endpoint_tag),
        provider_slug="openrouter",
        provider_type="openrouter",
        endpoint_tag=endpoint_tag,
        model_metadata=json.dumps(
            {"id": model_id, "pricing": pricing or _ENDPOINT_PRICING, **limits}
        ),
        upstream_provider_id=1,
    )


def _session_with_rows(rows: list[Any], override: Any = None) -> MagicMock:
    """Answers the path-row query with ``rows``, the override one with ``override``."""
    session = MagicMock()
    session.exec = AsyncMock(
        return_value=MagicMock(
            all=MagicMock(return_value=rows), first=MagicMock(return_value=override)
        )
    )
    return session


def _endpoint_selector() -> Any:
    selector = decode_model_path(
        encode_model_path(_OPENROUTER, MODEL_ID, "deepinfra/fp8")
    )
    assert selector is not None
    return selector


def _openrouter_upstream() -> MagicMock:
    upstream = _make_upstream(1)
    upstream.base_url = _OPENROUTER
    upstream.provider_fee = 1.0
    return upstream


@pytest.mark.asyncio
async def test_endpoint_pin_bills_the_endpoint_pricing() -> None:
    """A pinned endpoint is reserved and billed at the rates
    ``/v1/models/paths`` quotes for it, not the model's default listing."""
    model = _priced_model()
    upstream = _openrouter_upstream()
    request = _make_request(
        {
            "authorization": "Bearer sk-mpkey",
            "x-routstr-model-path": encode_model_path(
                _OPENROUTER, MODEL_ID, "deepinfra/fp8"
            ),
        },
        json.dumps({"model": MODEL_ID}).encode(),
    )

    with patch("routstr.payment.price.SATS_USD_PRICE", _SATS_USD):
        await _run_proxy(
            request, [(model, upstream)], session=_session_with_rows([_endpoint_row()])
        )

    billed = upstream.forward_request.await_args.args[7]
    assert billed.sats_pricing.prompt == pytest.approx(2e-6 / _SATS_USD)
    assert billed.sats_pricing.completion == pytest.approx(4e-6 / _SATS_USD)
    assert billed.sats_pricing.max_cost > model.sats_pricing.max_cost
    assert model.sats_pricing.prompt == pytest.approx(1e-6 / _SATS_USD)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [[], [_endpoint_row(model_id="other-model")]],
    ids=["no-stored-path", "other-model"],
)
async def test_endpoint_pin_without_a_stored_path_keeps_model_pricing(
    rows: list[Any],
) -> None:
    model = _priced_model()
    with patch("routstr.payment.price.SATS_USD_PRICE", _SATS_USD):
        priced = await proxy_module._price_pinned_endpoint(
            _session_with_rows(rows),
            _endpoint_selector(),
            model,
            _openrouter_upstream(),
        )
    assert priced is model


@pytest.mark.asyncio
async def test_endpoint_pin_without_sats_price_keeps_model_pricing() -> None:
    model = _priced_model()
    session = _session_with_rows([_endpoint_row()])
    with patch("routstr.payment.price.SATS_USD_PRICE", None):
        priced = await proxy_module._price_pinned_endpoint(
            session,
            _endpoint_selector(),
            model,
            _openrouter_upstream(),
        )
    assert priced is model
    session.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_pin_reserves_the_max_cost_paths_quotes() -> None:
    """The reservation uses the endpoint's own context and completion limits,
    the same ones ``/v1/models/paths`` quotes its max cost from."""
    from routstr.upstream.model_paths import _serialize_path

    row = _endpoint_row(context_length=32768, max_completion_tokens=8192)
    with patch("routstr.payment.price.SATS_USD_PRICE", _SATS_USD):
        priced = await proxy_module._price_pinned_endpoint(
            _session_with_rows([row]),
            _endpoint_selector(),
            _priced_model(),
            _openrouter_upstream(),
        )
        quoted = _serialize_path(row, 1.0)["model"]["sats_pricing"]["max_cost"]

    assert priced.sats_pricing is not None and priced.top_provider is not None
    assert priced.sats_pricing.max_cost == pytest.approx(quoted)
    assert priced.top_provider.max_completion_tokens == 8192


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "expected"),
    [
        # The operator's override is above the endpoint: it stays the price.
        ((6e-6, 12e-6), (6e-6, 12e-6)),
        # The endpoint costs more than the override: never bill below cost.
        ((1e-6, 2e-6), (2e-6, 4e-6)),
        # Each rate takes the higher of the two.
        ((3e-6, 1e-6), (3e-6, 4e-6)),
    ],
)
async def test_endpoint_pin_never_bills_below_an_operator_override(
    override: tuple[float, float], expected: tuple[float, float]
) -> None:
    from routstr.core.db import ModelRow

    model = _priced_model(*override)
    with patch("routstr.payment.price.SATS_USD_PRICE", _SATS_USD):
        priced = await proxy_module._price_pinned_endpoint(
            _session_with_rows([_endpoint_row()], override=MagicMock(spec=ModelRow)),
            _endpoint_selector(),
            model,
            _openrouter_upstream(),
        )

    assert (priced.pricing.prompt, priced.pricing.completion) == pytest.approx(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "endpoint", "expected_cache_read"),
    [
        # The override has no cache rate, so its cache reads bill at its
        # prompt rate; the endpoint's cheaper cache rate must not undercut it.
        (
            (2e-6, 8e-6, 0.0),
            {"prompt": 1e-6, "completion": 2e-6, "input_cache_read": 1e-7},
            2e-6,
        ),
        # The endpoint has no cache rate, so it charges its prompt rate; the
        # override's cheaper cache rate must not bill below that cost.
        ((1e-6, 2e-6, 1e-8), {"prompt": 2e-6, "completion": 4e-6}, 2e-6),
    ],
    ids=["override-without-cache-rate", "endpoint-without-cache-rate"],
)
async def test_override_floor_compares_effective_cache_rates(
    override: tuple[float, float, float],
    endpoint: dict[str, float],
    expected_cache_read: float,
) -> None:
    """A zero cache rate is billed at the prompt rate, so the floor compares
    those effective rates rather than the zeros."""
    from routstr.core.db import ModelRow

    with patch("routstr.payment.price.SATS_USD_PRICE", _SATS_USD):
        priced = await proxy_module._price_pinned_endpoint(
            _session_with_rows(
                [_endpoint_row(pricing=endpoint)], override=MagicMock(spec=ModelRow)
            ),
            _endpoint_selector(),
            _priced_model(*override),
            _openrouter_upstream(),
        )

    assert priced.pricing.input_cache_read == pytest.approx(expected_cache_read)
