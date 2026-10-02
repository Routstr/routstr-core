"""First-token / idle stream guards and the per-(provider, model) cooldown."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from routstr.core.error_scope import (
    ERROR_SCOPE_HEADER,
    ERROR_SCOPE_NODE,
    ERROR_SCOPE_UPSTREAM,
)
from routstr.core.exceptions import UpstreamError
from routstr.core.settings import Settings, settings
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.cooldown import is_cooling_down, record_failure
from routstr.upstream.stream_timeout import open_guarded_stream


def _response(chunks: AsyncIterator[bytes]) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.aiter_bytes = MagicMock(return_value=chunks)
    response.aclose = AsyncMock()
    return response


async def _never() -> AsyncIterator[bytes]:
    await asyncio.sleep(10)
    yield b"late"


async def _stalls_after_first() -> AsyncIterator[bytes]:
    yield b"first"
    await asyncio.sleep(10)
    yield b"never delivered"


async def _heartbeat_only(frame: bytes = b": keepalive\n\n") -> AsyncIterator[bytes]:
    while True:
        yield frame
        await asyncio.sleep(0.002)


@pytest.fixture
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "upstream_first_token_timeout_seconds", 0.01)
    monkeypatch.setattr(settings, "upstream_stream_idle_timeout_seconds", 0.01)


@pytest.mark.asyncio
async def test_first_token_timeout_closes_response_and_raises(
    fast_timeouts: None,
) -> None:
    response = _response(_never())

    with pytest.raises(UpstreamError) as exc_info:
        await open_guarded_stream(response, "test")

    assert exc_info.value.code == "UPSTREAM_TIMEOUT"
    assert exc_info.value.from_upstream_response is False
    response.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_stream_times_out_before_response_is_handed_off(
    fast_timeouts: None,
) -> None:
    provider = BaseUpstreamProvider(base_url="https://slow.example", api_key="test")
    response = _response(_never())

    with pytest.raises(UpstreamError, match="no first chunk"):
        await provider._generic_streaming_response(
            response, "key-hash", 100, "audio/speech", None, None, MagicMock()
        )

    response.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_stream_idle_abort_settles_without_clean_completion(
    fast_timeouts: None,
) -> None:
    provider = BaseUpstreamProvider(base_url="https://slow.example", api_key="test")
    finalize = AsyncMock()
    provider._finalize_generic_streaming_payment = finalize  # type: ignore[method-assign]
    upstream = _response(_stalls_after_first())
    upstream.status_code = 200
    upstream.headers = {}
    response = await provider._generic_streaming_response(
        upstream, "key-hash", 100, "audio/speech", None, None, MagicMock()
    )
    chunks = []
    with pytest.raises(UpstreamError, match="stream stalled"):
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    assert chunks == [b"first"]
    finalize.assert_awaited_once()
    upstream.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [b": keepalive\n\n", b"data: \n\n"])
async def test_sse_heartbeats_do_not_satisfy_first_token_timeout(
    fast_timeouts: None, frame: bytes
) -> None:
    response = _response(_heartbeat_only(frame))
    with pytest.raises(UpstreamError, match="no first chunk"):
        await open_guarded_stream(response, "test", sse=True)
    response.aclose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [b": keepalive\n\n", b"data: \n\n"])
async def test_sse_heartbeats_do_not_reset_idle_timeout(
    fast_timeouts: None, frame: bytes
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'data: {"delta":"first"}\n\n'
        async for chunk in _heartbeat_only(frame):
            yield chunk

    failures = MagicMock()
    stream = await open_guarded_stream(
        _response(chunks()), "test", sse=True, on_idle_timeout=failures
    )
    assert [chunk async for chunk in stream] == [b'data: {"delta":"first"}\n\n']
    assert stream.timed_out is True
    failures.assert_called_once()


@pytest.mark.asyncio
async def test_zero_first_token_timeout_disables_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_first_token_timeout_seconds", 0)
    monkeypatch.setattr(settings, "upstream_stream_idle_timeout_seconds", 0)

    async def _slow() -> AsyncIterator[bytes]:
        await asyncio.sleep(0.02)
        yield b"first"

    stream = await open_guarded_stream(_response(_slow()), "test")

    assert [chunk async for chunk in stream] == [b"first"]


def test_stream_guards_are_off_by_default() -> None:
    # Reasoning models can think silently for minutes; on by default, the
    # guards would fail requests that succeed without them.
    fields = Settings.__fields__
    assert fields["upstream_first_token_timeout_seconds"].default == 0
    assert fields["upstream_stream_idle_timeout_seconds"].default == 0


@pytest.mark.asyncio
async def test_idle_timeout_cools_down_the_serving_provider(
    fast_timeouts: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 1)
    provider = BaseUpstreamProvider(base_url="https://slow.example", api_key="test")
    provider.db_id = 17
    model = MagicMock(id="test-model")
    guarded = await provider._guard_stream(
        _response(_stalls_after_first()), model, sse=False
    )

    assert [chunk async for chunk in guarded] == [b"first"]
    assert is_cooling_down("db:17", "test-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_before_stall", [False, True])
async def test_responses_idle_timeout_does_not_emit_completed(
    fast_timeouts: None, terminal_before_stall: bool
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        event = (
            b'data: {"type":"response.completed","response":{"model":"test","usage":{"input_tokens":0,"output_tokens":1}}}\n\n'
            if terminal_before_stall
            else b'data: {"type":"response.created","response":{"model":"test"}}\n\n'
        )
        yield event
        await asyncio.sleep(10)

    response = _response(chunks())
    response.status_code = 200
    response.headers = {"content-type": "text/event-stream"}
    key = MagicMock()
    key.hashed_key = "test-key"
    key.balance = 1000
    session = MagicMock()
    session.get = AsyncMock(return_value=key)
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    provider = BaseUpstreamProvider(base_url="https://slow.example", api_key="test")

    with (
        patch("routstr.upstream.base.create_session", return_value=session_context),
        patch(
            "routstr.upstream.base.adjust_payment_for_tokens",
            AsyncMock(return_value={"input_tokens": 0, "output_tokens": 1}),
        ),
    ):
        result = await provider.handle_streaming_responses_completion(
            response, key, 100, reservation_snapshot=MagicMock()
        )
        emitted = b"".join(
            [
                chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                async for chunk in result.body_iterator
            ]
        )

    assert b'"type": "response.failed"' in emitted
    assert b'"code": "UPSTREAM_TIMEOUT"' in emitted
    assert b'"type": "response.completed"' not in emitted
    response.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_guarded_stream_passes_every_chunk_through() -> None:
    async def _chunks() -> AsyncIterator[bytes]:
        yield b"a"
        yield b"b"
        yield b"c"

    stream = await open_guarded_stream(_response(_chunks()), "test")

    assert [chunk async for chunk in stream] == [b"a", b"b", b"c"]


def test_cooldown_opens_after_allowed_fails_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 3)
    monkeypatch.setattr(settings, "upstream_cooldown_seconds", 30)

    for _ in range(2):
        record_failure("https://a.example", "m")
    assert is_cooling_down("https://a.example", "m") is False

    record_failure("https://a.example", "m")
    assert is_cooling_down("https://a.example", "m") is True
    # Scoped to the exact pair.
    assert is_cooling_down("https://b.example", "m") is False
    assert is_cooling_down("https://a.example", "other") is False

    with patch("routstr.upstream.cooldown.time.monotonic", return_value=1e6):
        assert is_cooling_down("https://a.example", "m") is False


def test_zero_cooldown_disables_skipping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 1)
    monkeypatch.setattr(settings, "upstream_cooldown_seconds", 0)

    record_failure("https://a.example", "m")

    assert is_cooling_down("https://a.example", "m") is False


def _proxy_request() -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    request.headers = {"authorization": "Bearer sk-key"}
    request.body = AsyncMock(return_value=b'{"model": "test-model", "stream": true}')
    request.state = MagicMock()
    request.state.request_id = "req-1"
    return request


def _upstream(base_url: str, forward: AsyncMock) -> MagicMock:
    upstream = MagicMock()
    upstream.provider_type = "test"
    upstream.base_url = base_url
    upstream.db_id = None
    upstream.prepare_headers = MagicMock(side_effect=lambda h: h)
    upstream.forward_request = forward
    return upstream


async def _run_proxy(
    candidates: list[tuple[MagicMock, MagicMock]],
    revert_mock: AsyncMock,
    request: MagicMock | None = None,
) -> Any:
    from routstr import proxy as proxy_module
    from routstr.auth import ReservationSnapshot
    from routstr.core.db import ApiKey

    key = ApiKey(hashed_key="streamkey", balance=10_000)
    reservation = ReservationSnapshot(
        release_id="release",
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
        patch.object(proxy_module, "revert_pay_for_request", revert_mock),
    ):
        request = request or _proxy_request()
        return await proxy_module._proxy(
            request, "v1/chat/completions", MagicMock(), await request.body()
        )


@pytest.mark.asyncio
async def test_first_token_timeout_fails_over_to_the_next_candidate(
    fast_timeouts: None,
) -> None:
    async def _timing_out(*args: Any, **kwargs: Any) -> Any:
        return await open_guarded_stream(_response(_never()), "test")

    served = MagicMock()
    served.status_code = 200
    slow = _upstream("https://slow.example", AsyncMock(side_effect=_timing_out))
    fast = _upstream("https://fast.example", AsyncMock(return_value=served))
    revert_mock = AsyncMock(return_value=True)

    response = await _run_proxy([(MagicMock(), slow), (MagicMock(), fast)], revert_mock)

    assert response is served
    fast.forward_request.assert_awaited_once()
    # The reservation carries over to the candidate that served the request.
    revert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_token_timeout_on_last_candidate_reverts_reservation(
    fast_timeouts: None,
) -> None:
    async def _timing_out(*args: Any, **kwargs: Any) -> Any:
        return await open_guarded_stream(_response(_never()), "test")

    slow = _upstream("https://slow.example", AsyncMock(side_effect=_timing_out))
    revert_mock = AsyncMock(return_value=True)

    response = await _run_proxy([(MagicMock(), slow)], revert_mock)

    assert response.status_code == 424
    assert json.loads(bytes(response.body))["error"]["code"] == "UPSTREAM_TIMEOUT"
    revert_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_cooling_down_candidate_is_skipped_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 1)
    monkeypatch.setattr(settings, "upstream_cooldown_seconds", 30)

    sick_response = MagicMock()
    sick_response.status_code = 200
    healthy_response = MagicMock()
    healthy_response.status_code = 200
    sick = _upstream("https://sick.example", AsyncMock(return_value=sick_response))
    healthy = _upstream("https://ok.example", AsyncMock(return_value=healthy_response))
    candidates = [(MagicMock(), sick), (MagicMock(), healthy)]

    record_failure("test|https://sick.example", "test-model")
    assert await _run_proxy(candidates, AsyncMock()) is healthy_response
    sick.forward_request.assert_not_awaited()

    with patch("routstr.upstream.cooldown.time.monotonic", return_value=1e6):
        assert await _run_proxy(candidates, AsyncMock()) is sick_response


@pytest.mark.asyncio
async def test_cooldown_never_empties_the_candidate_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 1)
    monkeypatch.setattr(settings, "upstream_cooldown_seconds", 30)

    only_response = MagicMock()
    only_response.status_code = 200
    only = _upstream("https://only.example", AsyncMock(return_value=only_response))

    record_failure("test|https://only.example", "test-model")

    assert await _run_proxy([(MagicMock(), only)], AsyncMock()) is only_response


@pytest.mark.asyncio
async def test_cooldown_distinguishes_credentials_at_same_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 1)
    bad = _upstream("https://same.example", AsyncMock())
    bad.db_id = 1
    good_response = MagicMock(status_code=200)
    good = _upstream("https://same.example", AsyncMock(return_value=good_response))
    good.db_id = 2
    other = _upstream("https://other.example", AsyncMock())
    record_failure("db:1", "test-model")

    assert (
        await _run_proxy(
            [(MagicMock(), bad), (MagicMock(), good), (MagicMock(), other)], AsyncMock()
        )
        is good_response
    )
    bad.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_normalizes_model_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 1)
    bad = _upstream("https://bad.example", AsyncMock())
    good_response = MagicMock(status_code=200)
    good = _upstream("https://good.example", AsyncMock(return_value=good_response))
    record_failure("test|https://bad.example", "test-model")
    request = _proxy_request()
    request.body = AsyncMock(
        return_value=b'{"model":"TEST-MODEL-20251222","stream":true}'
    )

    assert (
        await _run_proxy(
            [(MagicMock(id="test-model"), bad), (MagicMock(id="test-model"), good)],
            AsyncMock(),
            request,
        )
        is good_response
    )
    bad.forward_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_x_cashu_upstream_failure_opens_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 1)
    upstream = _upstream("https://cashu.example", AsyncMock())
    upstream.handle_x_cashu = AsyncMock(
        return_value=MagicMock(
            status_code=503, headers={ERROR_SCOPE_HEADER: ERROR_SCOPE_UPSTREAM}
        )
    )
    request = _proxy_request()
    request.headers = {"x-cashu": "token"}

    response = await _run_proxy([(MagicMock(), upstream)], AsyncMock(), request)

    assert response.status_code == 503
    assert is_cooling_down("test|https://cashu.example", "test-model")


@pytest.mark.asyncio
async def test_x_cashu_local_mint_failure_does_not_cool_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 1)
    upstream = _upstream("https://cashu.example", AsyncMock())
    upstream.handle_x_cashu = AsyncMock(
        return_value=MagicMock(status_code=503, headers={})
    )
    request = _proxy_request()
    request.headers = {"x-cashu": "token"}

    response = await _run_proxy([(MagicMock(), upstream)], AsyncMock(), request)

    assert response.status_code == 503
    assert not is_cooling_down("test|https://cashu.example", "test-model")


@pytest.mark.asyncio
async def test_node_scoped_upstream_exception_does_not_cool_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "upstream_allowed_fails", 1)
    upstream = _upstream(
        "https://healthy.example",
        AsyncMock(
            side_effect=UpstreamError(
                "local fault", status_code=500, scope=ERROR_SCOPE_NODE
            )
        ),
    )

    response = await _run_proxy([(MagicMock(), upstream)], AsyncMock())

    assert response.status_code == 500
    assert not is_cooling_down("test|https://healthy.example", "test-model")
