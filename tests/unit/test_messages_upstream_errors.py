import os
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import litellm
import pytest
from litellm.exceptions import MidStreamFallbackError

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.core.exceptions import UpstreamError  # noqa: E402
from routstr.payment.models import Architecture, Model, Pricing  # noqa: E402
from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402
from routstr.upstream.messages_dispatch import (  # noqa: E402
    collapse_litellm_message,
)

_MIDSTREAM_FAILURE = MidStreamFallbackError(
    message="No credits.",
    model="x",
    llm_provider="openai",
    original_exception=litellm.APIError(
        status_code=500, message="No credits.", llm_provider="openai", model="x"
    ),
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("You have no credits remaining.", "You have no credits remaining."),
        # upstream_error_from_exception reads `.message`, which omits the
        # "Original exception:" chain that only `str()` appends.
        (_MIDSTREAM_FAILURE.message, "No credits."),
        (str(_MIDSTREAM_FAILURE), "No credits."),
        ("x" * 301, "x" * 299 + "…"),
    ],
)
def test_collapse_litellm_message(message: str, expected: str) -> None:
    assert collapse_litellm_message(message) == expected


_RATE_LIMIT = litellm.RateLimitError(
    message=(
        "Rate limit reached for gpt-4o on tokens per min (TPM): Limit 30000, "
        "Used 29000, Requested 2000. Please try again in 1.2s."
    ),
    llm_provider="openai",
    model="gpt-4o",
)
_BAD_REQUEST = litellm.BadRequestError(
    message="context length exceeded", model="gpt-4o", llm_provider="openai"
)

_MID_STREAM_CASES = [
    pytest.param(_RATE_LIMIT, 429, "UPSTREAM_RATE_LIMIT", id="rate-limit"),
    pytest.param(_BAD_REQUEST, 400, None, id="bad-request"),
    pytest.param(_MIDSTREAM_FAILURE, 500, None, id="midstream-fallback"),
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
    assert "litellm." not in str(err)


@pytest.mark.asyncio
@pytest.mark.parametrize(("exc", "status_code", "code"), _MID_STREAM_CASES)
async def test_non_streaming_aggregation_surfaces_mid_stream_failure(
    exc: Exception, status_code: int, code: str | None
) -> None:
    async def fake_acreate(**kwargs: Any) -> AsyncIterator[dict]:
        return _failing_stream(exc)

    with (
        patch(
            "litellm.anthropic.messages.acreate",
            new=AsyncMock(side_effect=fake_acreate),
        ),
        pytest.raises(UpstreamError) as exc_info,
    ):
        await BaseUpstreamProvider(
            base_url="http://test", api_key="k"
        )._dispatch_anthropic_messages(
            request_body=b'{"messages": [], "max_tokens": 8, "stream": false}',
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
        await provider._stream_x_cashu_litellm_messages(
            _failing_stream(exc),
            amount=5_000,
            unit="sat",
            max_cost_for_model=10_000,
            requested_model="gpt-4o",
            mint=None,
            request_id="req-test",
        )

    _assert_upstream_error(exc_info.value, status_code, code)
