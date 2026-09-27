"""First-token / idle stream guards and the per-(provider, model) cooldown."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from routstr.core.exceptions import UpstreamError
from routstr.core.settings import settings
from routstr.upstream.cooldown import is_cooling_down, record_failure, reset_cooldowns
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


@pytest.fixture(autouse=True)
def _clean_cooldowns() -> Any:
    reset_cooldowns()
    yield
    reset_cooldowns()


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


@pytest.mark.asyncio
async def test_idle_timeout_ends_the_stream_without_raising(
    fast_timeouts: None,
) -> None:
    stream = await open_guarded_stream(_response(_stalls_after_first()), "test")

    # The stalled stream ends after the delivered bytes; the caller's finalizer
    # then settles actual usage instead of the request hanging.
    assert [chunk async for chunk in stream] == [b"first"]


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
    upstream.prepare_headers = MagicMock(side_effect=lambda h: h)
    upstream.forward_request = forward
    return upstream


async def _run_proxy(
    candidates: list[tuple[MagicMock, MagicMock]],
    revert_mock: AsyncMock,
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
        return await proxy_module.proxy(
            _proxy_request(), "v1/chat/completions", session=MagicMock()
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

    record_failure("https://sick.example", "test-model")
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

    record_failure("https://only.example", "test-model")

    assert await _run_proxy([(MagicMock(), only)], AsyncMock()) is only_response
