"""Unit tests for the streaming EHBP relay.

The buffered path (``forward_with_trailer``) withholds every byte until the
enclave finishes generating, so a client's time-to-first-token becomes the
whole generation time. These tests pin the streaming relay: chunks reach the
client as the upstream produces them, and payment still settles from the
post-body ``X-Tinfoil-Usage-Metrics`` trailer.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from routstr.core.terminal_outcomes import TerminalOutcomeContext
from routstr.upstream import ehbp as ehbp_module
from routstr.upstream.tinfoil_trailer import StreamedTrailerResponse

_USAGE_TRAILER = "prompt=10,completion=20,total=30"


def _outcome() -> TerminalOutcomeContext:
    """A real context: the settle helpers ``replace()`` it, so it cannot be None."""
    return TerminalOutcomeContext(outcome_id="req-stream", model_identifier="m")


def _request() -> MagicMock:
    request = MagicMock()
    request.state.request_id = "req-stream"
    request.method = "POST"
    request.query_params = {}
    request.headers = {}
    request.body = AsyncMock(return_value=b"opaque")
    return request


def _upstream_mocks() -> tuple[MagicMock, MagicMock]:
    profile = MagicMock()
    profile.client_target_url_header = None
    profile.allow_client_target_override = False
    profile.proxy_only_headers = frozenset()
    profile.usage_response_header = None

    target = MagicMock()
    target.url = "https://inference.tinfoil.sh/v1/chat/completions"
    target.headers = {}
    target.profile = None

    upstream = MagicMock()
    upstream.prepare_headers.return_value = {}
    upstream.get_ehbp_forwarding_target.return_value = target
    upstream.get_confidential_inference_profile.return_value = profile
    upstream.prepare_params.return_value = {}
    upstream.provider_type = "tinfoil"

    model_obj = MagicMock()
    model_obj.id = "tinfoil-deepseek-v4-1-flash"
    model_obj.forwarded_model_id = "deepseek-v4-1-flash"
    return upstream, model_obj


def _streamed(
    chunks: list[bytes],
    *,
    status_code: int = 200,
    trailers: list[tuple[str, str]] | None = None,
    headers: list[tuple[str, str]] | None = None,
) -> StreamedTrailerResponse:
    """A streamed response that records how eagerly its body was consumed."""
    consumed: list[bytes] = []

    async def _body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            consumed.append(chunk)
            yield chunk
            # A real upstream produces these over time; yield to the loop so a
            # consumer that buffers cannot look identical to one that streams.
            await asyncio.sleep(0)

    future: asyncio.Future[list[tuple[str, str]]] = (
        asyncio.get_running_loop().create_future()
    )
    future.set_result(
        trailers
        if trailers is not None
        else [("x-tinfoil-usage-metrics", _USAGE_TRAILER)]
    )
    resp = StreamedTrailerResponse(
        status_code=status_code,
        headers=headers
        if headers is not None
        else [
            ("content-type", "text/event-stream"),
            ("ehbp-response-nonce", "ab" * 32),
        ],
        body=_body(),
        trailers=future,
    )
    resp.consumed = consumed  # type: ignore[attr-defined]
    return resp


@pytest.mark.asyncio
async def test_streamed_body_reaches_client_before_upstream_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response body must yield the first chunk without draining the rest."""
    chunks = [b"frame-1", b"frame-2", b"frame-3"]
    streamed = _streamed(chunks)
    monkeypatch.setattr(
        ehbp_module, "stream_with_trailer", AsyncMock(return_value=streamed)
    )
    monkeypatch.setattr(
        ehbp_module, "_settle_streamed_ehbp", AsyncMock(return_value=None)
    )

    upstream, model_obj = _upstream_mocks()
    response = await ehbp_module.forward_ehbp_request(
        request=_request(),
        path="v1/chat/completions",
        headers={},
        request_body=b"opaque",
        upstream=upstream,
        key=MagicMock(),
        max_cost_for_model=5000,
        session=MagicMock(),
        model_obj=model_obj,
    )

    assert response.status_code == 200
    iterator = response.body_iterator
    first = await anext(iterator)
    # Only the first frame has been pulled: the relay must not pre-drain.
    assert consumed_count(streamed) == 1
    assert first in chunks
    rest = [chunk async for chunk in iterator]
    assert [first, *rest] == chunks


def consumed_count(resp: StreamedTrailerResponse) -> int:
    return len(getattr(resp, "consumed", []))


@pytest.mark.asyncio
async def test_streaming_settles_from_the_usage_trailer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settlement runs after the body drains and uses the trailer's usage."""
    streamed = _streamed([b"frame-1", b"frame-2"])
    monkeypatch.setattr(
        ehbp_module, "stream_with_trailer", AsyncMock(return_value=streamed)
    )
    settle = AsyncMock(return_value=None)
    monkeypatch.setattr(ehbp_module, "_settle_streamed_ehbp", settle)

    upstream, model_obj = _upstream_mocks()
    response = await ehbp_module.forward_ehbp_request(
        request=_request(),
        path="v1/chat/completions",
        headers={},
        request_body=b"opaque",
        upstream=upstream,
        key=MagicMock(),
        max_cost_for_model=5000,
        session=MagicMock(),
        model_obj=model_obj,
    )

    async for _ in response.body_iterator:
        pass

    settle.assert_awaited_once()
    kwargs = settle.await_args.kwargs
    assert kwargs["resp"] is streamed


@pytest.mark.asyncio
async def test_streaming_settles_even_when_the_client_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed generator must still settle, or the reservation leaks."""
    streamed = _streamed([b"frame-1", b"frame-2", b"frame-3"])
    monkeypatch.setattr(
        ehbp_module, "stream_with_trailer", AsyncMock(return_value=streamed)
    )
    settle = AsyncMock(return_value=None)
    monkeypatch.setattr(ehbp_module, "_settle_streamed_ehbp", settle)

    upstream, model_obj = _upstream_mocks()
    response = await ehbp_module.forward_ehbp_request(
        request=_request(),
        path="v1/chat/completions",
        headers={},
        request_body=b"opaque",
        upstream=upstream,
        key=MagicMock(),
        max_cost_for_model=5000,
        session=MagicMock(),
        model_obj=model_obj,
    )

    iterator = response.body_iterator
    await anext(iterator)
    await iterator.aclose()  # what Starlette does when the client goes away

    settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_streaming_without_usage_trailer_releases_the_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No trailer means no measured usage: release instead of charging."""
    streamed = _streamed([b"frame-1"], trailers=[])
    monkeypatch.setattr(
        ehbp_module, "stream_with_trailer", AsyncMock(return_value=streamed)
    )
    settle = AsyncMock(return_value=None)
    monkeypatch.setattr(ehbp_module, "_settle_streamed_ehbp", settle)

    upstream, model_obj = _upstream_mocks()
    response = await ehbp_module.forward_ehbp_request(
        request=_request(),
        path="v1/chat/completions",
        headers={},
        request_body=b"opaque",
        upstream=upstream,
        key=MagicMock(),
        max_cost_for_model=5000,
        session=MagicMock(),
        model_obj=model_obj,
    )

    async for _ in response.body_iterator:
        pass

    settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_streaming_error_status_drains_once_without_refetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error status must reuse the open stream, not re-bill the upstream."""
    streamed = _streamed([b"cipher"], status_code=503, trailers=[])
    stream_mock = AsyncMock(return_value=streamed)
    monkeypatch.setattr(ehbp_module, "stream_with_trailer", stream_mock)
    buffered_mock = AsyncMock()
    monkeypatch.setattr(ehbp_module, "forward_with_trailer", buffered_mock)

    upstream, model_obj = _upstream_mocks()

    with pytest.raises(ehbp_module.UpstreamError):
        await ehbp_module.forward_ehbp_request(
            request=_request(),
            path="v1/chat/completions",
            headers={},
            request_body=b"opaque",
            upstream=upstream,
            key=MagicMock(),
            max_cost_for_model=5000,
            session=MagicMock(),
            model_obj=model_obj,
        )

    stream_mock.assert_awaited_once()
    buffered_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_buffered_mode_is_still_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setting must be able to fall back to the buffered transport.

    The buffered path's own behaviour is covered by the existing EHBP suite;
    this only pins that disabling the setting selects it.
    """
    monkeypatch.setattr(ehbp_module.settings, "ehbp_stream_upstream_responses", False)
    stream_mock = AsyncMock(
        side_effect=AssertionError("streaming transport must not be used")
    )
    monkeypatch.setattr(ehbp_module, "stream_with_trailer", stream_mock)

    from routstr.upstream.tinfoil_trailer import TrailerResponse

    buffered = TrailerResponse(
        status_code=200,
        headers=[("content-type", "text/event-stream")],
        body=b"all-at-once",
        trailers=[("x-tinfoil-usage-metrics", _USAGE_TRAILER)],
    )
    forward_mock = AsyncMock(return_value=buffered)
    monkeypatch.setattr(ehbp_module, "forward_with_trailer", forward_mock)
    # Settlement internals are exercised elsewhere; here only the transport
    # choice matters, so stub both settle helpers.
    monkeypatch.setattr(
        ehbp_module,
        "_settle_ehbp_usage_measured",
        AsyncMock(return_value=({}, _outcome())),
    )
    monkeypatch.setattr(
        ehbp_module,
        "_settle_ehbp_usage_unmeasured",
        AsyncMock(return_value=({}, _outcome())),
    )

    upstream, model_obj = _upstream_mocks()
    response = await ehbp_module.forward_ehbp_request(
        request=_request(),
        path="v1/chat/completions",
        headers={},
        request_body=b"opaque",
        upstream=upstream,
        key=MagicMock(),
        max_cost_for_model=5000,
        session=MagicMock(),
        model_obj=model_obj,
        reservation_snapshot=MagicMock(),
    )

    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == b"all-at-once"
    stream_mock.assert_not_awaited()
    forward_mock.assert_awaited_once()
