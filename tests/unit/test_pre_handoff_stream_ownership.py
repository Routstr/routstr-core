import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.responses import StreamingResponse

from routstr.upstream.base import BaseUpstreamProvider


async def _chunks() -> AsyncIterator[bytes]:
    yield b"chunk"


def _forwarding_case() -> tuple[
    BaseUpstreamProvider,
    MagicMock,
    MagicMock,
    MagicMock,
    MagicMock,
    MagicMock,
    MagicMock,
]:
    provider = BaseUpstreamProvider("https://api.example.com", "test-key")
    request = MagicMock()
    request.method = "POST"
    request.query_params = {}
    key = MagicMock()
    key.hashed_key = "key-hash"
    session = MagicMock()
    model = MagicMock()
    model.forwarded_model_id = None
    model.id = "model"

    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {"content-type": "application/octet-stream"}
    response.aclose = AsyncMock()
    response.aiter_bytes = MagicMock(side_effect=_chunks)

    client = MagicMock()
    client.build_request.return_value = MagicMock()
    client.send = AsyncMock(return_value=response)
    return provider, request, key, session, model, response, client


async def _forward(
    method_name: str,
    *,
    reservation_snapshot: object | None,
) -> tuple[StreamingResponse, MagicMock, BaseUpstreamProvider]:
    provider, request, key, session, model, response, client = _forwarding_case()
    prepare_method = (
        "prepare_request_body"
        if method_name == "forward_request"
        else "prepare_responses_request_body"
    )

    with (
        patch("routstr.upstream.base._acquire_upstream_client", return_value=client),
        patch.object(provider, "normalize_request_path", return_value="audio/speech"),
        patch.object(
            provider,
            "build_request_url",
            return_value="https://api.example.com/audio/speech",
        ),
        patch.object(provider, prepare_method, return_value=b"{}"),
        patch.object(provider, "prepare_params", return_value={}),
    ):
        result = await getattr(provider, method_name)(
            request=request,
            path="audio/speech",
            headers={},
            request_body=b"{}",
            key=key,
            max_cost_for_model=1_000,
            session=session,
            model_obj=model,
            reservation_snapshot=reservation_snapshot,
        )

    assert isinstance(result, StreamingResponse)
    return result, response, provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name", ["forward_request", "forward_responses_request"]
)
async def test_cancellation_before_stream_handoff_closes_response_once(
    method_name: str,
) -> None:
    provider, request, key, session, model, response, client = _forwarding_case()
    prepare_method = (
        "prepare_request_body"
        if method_name == "forward_request"
        else "prepare_responses_request_body"
    )
    lookup_started = asyncio.Event()

    async def wait_for_reservation(*_: object) -> None:
        lookup_started.set()
        await asyncio.Future()

    with (
        patch("routstr.upstream.base._acquire_upstream_client", return_value=client),
        patch.object(provider, "normalize_request_path", return_value="audio/speech"),
        patch.object(
            provider,
            "build_request_url",
            return_value="https://api.example.com/audio/speech",
        ),
        patch.object(provider, prepare_method, return_value=b"{}"),
        patch.object(provider, "prepare_params", return_value={}),
        patch(
            "routstr.upstream.base.get_reservation_snapshot",
            side_effect=wait_for_reservation,
        ),
    ):
        task = asyncio.create_task(
            getattr(provider, method_name)(
                request=request,
                path="audio/speech",
                headers={},
                request_body=b"{}",
                key=key,
                max_cost_for_model=1_000,
                session=session,
                model_obj=model,
            )
        )
        await lookup_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    response.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name", ["forward_request", "forward_responses_request"]
)
async def test_successful_stream_handoff_does_not_close_response_early(
    method_name: str,
) -> None:
    result, response, provider = await _forward(
        method_name,
        reservation_snapshot=MagicMock(),
    )
    response.aclose.assert_not_awaited()

    iterator = cast(AsyncGenerator[bytes, None], result.body_iterator)
    with patch.object(
        provider,
        "_finalize_generic_streaming_payment",
        new=AsyncMock(),
    ):
        assert await anext(iterator) == b"chunk"
        await iterator.aclose()

    response.aclose.assert_awaited_once_with()
