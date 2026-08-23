"""Response-contract tests for Routstr cost metadata across paid paths."""

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.core.db import ApiKey  # noqa: E402
from routstr.upstream.base import BaseUpstreamProvider  # noqa: E402

COST_DATA = {
    "base_msats": 0,
    "input_msats": 1_200,
    "output_msats": 300,
    "total_msats": 1_500,
    "total_usd": 0.0001,
    "input_tokens": 10,
    "output_tokens": 3,
    "cache_read_input_tokens": 8,
    "cache_creation_input_tokens": 2,
    "cache_read_msats": 80,
    "cache_creation_msats": 40,
}


def _provider() -> BaseUpstreamProvider:
    return BaseUpstreamProvider(base_url="http://test", api_key="upstream-key")


def _key() -> ApiKey:
    return ApiKey(hashed_key="abcdef0123" * 4, balance=1_000_000)


def _session() -> Any:
    session = MagicMock()
    session.refresh = AsyncMock()
    return session


def _upstream_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "http://test"),
    )


def _assert_cost_contract(response: Any) -> None:
    body = json.loads(response.body)
    assert body["usage"]["cost"] == {
        "base_msats": 0,
        "input_msats": 1_200,
        "output_msats": 300,
        "total_msats": 1_500,
        "charged_msats": 1_500,
        "total_usd": 0.0001,
        "cache_read_input_tokens": 8,
        "cache_creation_input_tokens": 2,
        "cache_read_msats": 80,
        "cache_creation_msats": 40,
    }
    assert response.headers["X-Routstr-Cost-Msats"] == "1500"
    assert response.headers["X-Routstr-Input-Cost-Msats"] == "1200"
    assert response.headers["X-Routstr-Output-Cost-Msats"] == "300"


@pytest.mark.asyncio
async def test_duplicate_finalization_publishes_zero_debit_and_computed_usage() -> None:
    provider = _provider()
    duplicate_cost = {**COST_DATA, "charged_msats": 0}
    with patch(
        "routstr.upstream.base.adjust_payment_for_tokens",
        new=AsyncMock(return_value=duplicate_cost),
    ):
        response = await provider.handle_non_streaming_chat_completion(
            _upstream_response(
                {
                    "model": "test-model",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                }
            ),
            _key(),
            _session(),
            deducted_max_cost=10_000,
        )

    body = json.loads(response.body)
    assert response.headers["X-Routstr-Cost-Msats"] == "0"
    assert response.headers["X-Routstr-Computed-Cost-Msats"] == "1500"
    assert body["usage"]["cost"]["total_msats"] == 0
    assert body["usage"]["cost"]["charged_msats"] == 0
    assert body["usage"]["cost"]["computed_msats"] == 1_500
    assert body["cost"]["total_msats"] == 0
    assert body["cost"]["computed_msats"] == 1_500


@pytest.mark.asyncio
async def test_balance_chat_completion_uses_shared_cost_contract() -> None:
    provider = _provider()
    with patch(
        "routstr.upstream.base.adjust_payment_for_tokens",
        new=AsyncMock(return_value=dict(COST_DATA)),
    ):
        response = await provider.handle_non_streaming_chat_completion(
            _upstream_response(
                {
                    "model": "test-model",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                }
            ),
            _key(),
            _session(),
            deducted_max_cost=10_000,
        )

    _assert_cost_contract(response)


@pytest.mark.asyncio
async def test_balance_responses_completion_uses_shared_cost_contract() -> None:
    provider = _provider()
    with patch(
        "routstr.upstream.base.adjust_payment_for_tokens",
        new=AsyncMock(return_value=dict(COST_DATA)),
    ):
        response = await provider.handle_non_streaming_responses_completion(
            _upstream_response(
                {
                    "model": "test-model",
                    "usage": {"input_tokens": 10, "output_tokens": 3},
                }
            ),
            _key(),
            _session(),
            deducted_max_cost=10_000,
        )

    _assert_cost_contract(response)
