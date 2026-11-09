from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest
from fastapi import Response

from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.routstr import RoutstrUpstreamProvider


class DummyAsyncClient:
    def __init__(self, response: Mock | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False

    async def get(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> Mock:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@pytest.mark.asyncio
async def test_get_balance_omits_auth_header_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Mock()
    response.json.return_value = {"balance_msats": 42000}
    response.raise_for_status.return_value = None

    client = DummyAsyncClient(response=response)
    monkeypatch.setattr("routstr.upstream.routstr.httpx.AsyncClient", lambda: client)

    provider = RoutstrUpstreamProvider(base_url="https://node.example", api_key="")

    balance = await provider.get_balance()

    assert balance == 42.0
    assert client.calls == [
        {
            "url": "https://node.example/v1/balance/info",
            "headers": {},
            "timeout": 10.0,
        }
    ]


@pytest.mark.asyncio
async def test_get_balance_returns_none_on_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DummyAsyncClient(error=httpx.ConnectTimeout("timed out"))
    monkeypatch.setattr("routstr.upstream.routstr.httpx.AsyncClient", lambda: client)

    provider = RoutstrUpstreamProvider(
        base_url="https://node.example",
        api_key="secret",
    )

    balance = await provider.get_balance()

    assert balance is None


def test_normalize_request_path_keeps_v1_prefix() -> None:
    """Routstr upstream stores ``base_url`` without ``/v1``; the prefix
    must stay on the path so ``build_request_url`` produces ``/v1/<endpoint>``
    instead of ``/<endpoint>`` (which the upstream Routstr 404s with HTML)."""
    provider = RoutstrUpstreamProvider(
        base_url="https://privateprovider.xyz", api_key="key"
    )

    assert provider.normalize_request_path("v1/messages") == "v1/messages"
    assert provider.normalize_request_path("/v1/messages") == "v1/messages"
    assert (
        provider.normalize_request_path("v1/chat/completions")
        == "v1/chat/completions"
    )


def test_build_request_url_for_v1_messages() -> None:
    """Forwarding ``/v1/messages`` must hit the upstream's ``/v1/messages``."""
    provider = RoutstrUpstreamProvider(
        base_url="https://privateprovider.xyz", api_key="key"
    )

    normalized = provider.normalize_request_path("v1/messages")

    assert (
        provider.build_request_url(normalized)
        == "https://privateprovider.xyz/v1/messages"
    )


def test_supports_anthropic_messages_natively() -> None:
    """Routstr nodes serve ``/v1/messages`` directly, so the proxy must
    forward as-is instead of round-tripping through litellm."""
    assert RoutstrUpstreamProvider.supports_anthropic_messages is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_responses_through_a_routstr_upstream_are_billed(stream: bool) -> None:
    provider = RoutstrUpstreamProvider(
        base_url="https://node.example", api_key="sk-upstream"
    )
    model = Model(
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
    request = MagicMock(method="POST", query_params={})
    request.state.request_id = None
    upstream = httpx.Response(
        200,
        content=b"data: {}\n\n" if stream else b'{"object": "response"}',
        headers={"content-type": "text/event-stream" if stream else "application/json"},
        request=httpx.Request("POST", "https://node.example/v1/responses"),
    )
    handler = (
        "handle_streaming_responses_completion"
        if stream
        else "handle_non_streaming_responses_completion"
    )

    with (
        patch("httpx.AsyncClient.send", AsyncMock(return_value=upstream)),
        patch.object(provider, handler, AsyncMock(return_value=Response())) as billed,
        patch.object(
            provider, "_generic_streaming_response", AsyncMock(return_value=Response())
        ) as unbilled,
    ):
        await provider.forward_responses_request(
            request,
            "v1/responses",
            {},
            b"{}",
            Mock(hashed_key="key-hash"),
            1000,
            AsyncMock(),
            model,
            reservation_snapshot=Mock(),
        )

    billed.assert_awaited_once()
    unbilled.assert_not_awaited()
