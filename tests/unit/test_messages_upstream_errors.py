import os
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.core.exceptions import UpstreamError  # noqa: E402
from routstr.payment.models import Architecture, Model, Pricing  # noqa: E402
from routstr.upstream.anthropic_openai_stream import (  # noqa: E402
    StreamPayloadError,
)
from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402
from routstr.upstream.messages_dispatch import (  # noqa: E402
    upstream_error_from_exception,
)
from routstr.upstream.messages_transport import UpstreamHTTPError  # noqa: E402

_RATE_LIMIT = UpstreamHTTPError(
    "Rate limit reached for gpt-4o on tokens per min (TPM): Limit 30000, "
    "Used 29000, Requested 2000. Please try again in 1.2s.",
    status_code=429,
)
_BAD_REQUEST = UpstreamHTTPError("context length exceeded", status_code=400)
_MIDSTREAM_FAILURE = StreamPayloadError("No credits.", 500)

_MID_STREAM_CASES = [
    pytest.param(_RATE_LIMIT, 429, "UPSTREAM_RATE_LIMIT", id="rate-limit"),
    pytest.param(_BAD_REQUEST, 400, None, id="bad-request"),
    pytest.param(_MIDSTREAM_FAILURE, 500, None, id="midstream-payload"),
]


def _make_model() -> Model:
    return Model(
        id="gpt-4o",
        name="gpt-4o",
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="x",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=0.0,
            completion=0.0,
            request=0.0,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
            max_cost=0.0,
        ),
    )


def _failing_stream(exc: Exception) -> AsyncIterator[dict]:
    async def gen() -> AsyncIterator[dict]:
        yield {
            "type": "message_start",
            "message": {"id": "msg_1", "model": "gpt-4o", "usage": {}},
        }
        raise exc

    return gen()


def _assert_upstream_error(
    err: UpstreamError, status_code: int, code: str | None
) -> None:
    assert err.status_code == status_code
    assert err.code == code
    assert err.from_upstream_response is True
    assert err.message.startswith("Upstream error: ")


def test_long_upstream_message_is_capped() -> None:
    err = upstream_error_from_exception(
        UpstreamHTTPError("x" * 301, status_code=500), log_message="test"
    )
    assert err.message == "Upstream error: " + "x" * 299 + "…"


def test_retry_after_header_reaches_rate_limit_details() -> None:
    err = upstream_error_from_exception(
        UpstreamHTTPError(
            "Rate limit reached for requests",
            status_code=429,
            headers=httpx.Headers({"retry-after": "7"}),
        ),
        log_message="test",
    )
    assert err.code == "UPSTREAM_RATE_LIMIT"
    assert err.details is not None
    assert err.details["retry_after_seconds"] == 7.0


@pytest.mark.parametrize(
    ("exc", "status_code"),
    [
        (httpx.PoolTimeout("busy"), 503),
        (httpx.ReadTimeout("slow"), 408),
        (httpx.ConnectError("refused"), 500),
    ],
)
def test_transport_errors_map_to_status(exc: Exception, status_code: int) -> None:
    err = upstream_error_from_exception(exc, log_message="test")
    assert err.status_code == status_code
    # Node-side failures: the proxy must not retry them in place.
    assert err.from_upstream_response is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "status_code", "code"), _MID_STREAM_CASES)
async def test_non_streaming_aggregation_surfaces_mid_stream_failure(
    exc: Exception, status_code: int, code: str | None
) -> None:
    async def fake_open_events(call: Any, **_: Any) -> AsyncIterator[dict]:
        return _failing_stream(exc)

    with (
        patch(
            "routstr.upstream.messages_dispatch.open_events",
            new=AsyncMock(side_effect=fake_open_events),
        ),
        pytest.raises(UpstreamError) as exc_info,
    ):
        await BaseUpstreamProvider(
            base_url="http://test", api_key="k"
        )._dispatch_anthropic_messages(
            request_body=b'{"messages": [{"role": "user", "content": "hi"}], '
            b'"max_tokens": 8, "stream": false}',
            model_obj=_make_model(),
        )

    _assert_upstream_error(exc_info.value, status_code, code)


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "status_code", "code"), _MID_STREAM_CASES)
async def test_x_cashu_buffered_stream_surfaces_mid_stream_failure(
    exc: Exception, status_code: int, code: str | None
) -> None:
    provider = BaseUpstreamProvider(base_url="http://test", api_key="k")

    with pytest.raises(UpstreamError) as exc_info:
        await provider._stream_x_cashu_translated_messages(
            _failing_stream(exc),
            amount=5_000,
            unit="sat",
            max_cost_for_model=10_000,
            requested_model="gpt-4o",
            mint=None,
            request_id="req-test",
        )

    _assert_upstream_error(exc_info.value, status_code, code)


@pytest.mark.asyncio
async def test_x_cashu_buffering_closes_upstream_on_unexpected_failure() -> None:
    """A non-provider failure mid-buffer still releases the pooled connection."""

    class Upstream:
        closed = False

        def __aiter__(self) -> "Upstream":
            return self

        async def __anext__(self) -> bytes:
            raise RuntimeError("annotate blew up")

        async def aclose(self) -> None:
            self.closed = True

    upstream = Upstream()
    with pytest.raises(RuntimeError):
        await BaseUpstreamProvider(
            base_url="http://test", api_key="k"
        )._stream_x_cashu_translated_messages(
            upstream,
            amount=5_000,
            unit="sat",
            max_cost_for_model=10_000,
            requested_model="gpt-4o",
            mint=None,
            request_id="req-test",
        )
    assert upstream.closed
