"""The up-front reservation covers the web searches a request may run.

Searches are billed per request at settlement on top of tokens. A reservation
sized on tokens alone lets a low balance key or a small X-Cashu token run
searches whose charge is then capped and written off.
"""

import os
from typing import Any
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr.core.settings import settings  # noqa: E402
from routstr.payment.helpers import (  # noqa: E402
    calculate_discounted_max_cost,
    reserved_web_searches,
)
from routstr.payment.models import Pricing  # noqa: E402

# Sats per search; 10 searches are 120_000 msat.
SEARCH_SATS = 12.0
MAX_COST_MSATS = 500_000


def _model(web_search: float) -> Mock:
    model_obj = Mock()
    model_obj.sats_pricing = Pricing(
        prompt=0.001,
        completion=0.002,
        web_search=web_search,
        max_prompt_cost=200.0,
        max_completion_cost=300.0,
        max_cost=MAX_COST_MSATS / 1000,
    )
    model_obj.top_provider = None
    model_obj.context_length = None
    return model_obj


def _body(*tools: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "What changed in the news today?"}],
        "max_tokens": 1024,
    }
    if tools:
        body["tools"] = list(tools)
    return body


async def _reservation(body: dict[str, Any], web_search: float) -> int:
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1),
        patch.object(settings, "web_search_default_max_uses", 8),
    ):
        return await calculate_discounted_max_cost(
            MAX_COST_MSATS, body, _model(web_search)
        )


async def _search_share(body: dict[str, Any]) -> int:
    """What the reservation adds for searches, prompt and completion held fixed."""
    return await _reservation(body, SEARCH_SATS) - await _reservation(body, 0.0)


@pytest.mark.asyncio
async def test_max_uses_searches_are_reserved() -> None:
    body = _body({"type": "web_search_20250305", "name": "web_search", "max_uses": 3})

    assert await _search_share(body) == 3 * SEARCH_SATS * 1000


@pytest.mark.asyncio
async def test_uncapped_search_tool_reserves_the_configured_default() -> None:
    body = _body({"type": "web_search_20250305", "name": "web_search"})

    assert await _search_share(body) == 8 * SEARCH_SATS * 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("max_uses", [0, -1, True, "5", None, 2.5])
async def test_invalid_max_uses_falls_back_to_the_default(max_uses: Any) -> None:
    body = _body(
        {"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}
    )

    assert await _search_share(body) == 8 * SEARCH_SATS * 1000


@pytest.mark.asyncio
async def test_every_search_tool_counts_and_other_tools_do_not() -> None:
    body = _body(
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 2},
        {"type": "web_search", "max_uses": 4},
        {"name": "get_weather", "input_schema": {"type": "object"}},
        {"type": "function", "function": {"name": "lookup"}},
    )

    assert await _search_share(body) == 6 * SEARCH_SATS * 1000


@pytest.mark.asyncio
async def test_no_search_tool_reserves_nothing_extra() -> None:
    body = _body({"name": "get_weather", "input_schema": {"type": "object"}})

    assert await _search_share(body) == 0


@pytest.mark.asyncio
async def test_unpriced_search_reserves_nothing_extra() -> None:
    body = _body({"type": "web_search_20250305", "name": "web_search"})

    # A free search adds nothing however many searches the tool may run.
    unpriced = await _reservation(body, 0.0)
    with patch.object(settings, "web_search_default_max_uses", 100):
        with (
            patch.object(settings, "fixed_pricing", False),
            patch.object(settings, "tolerance_percentage", 0),
            patch.object(settings, "min_request_msat", 1),
        ):
            many = await calculate_discounted_max_cost(
                MAX_COST_MSATS, body, _model(0.0)
            )
    assert many == unpriced


@pytest.mark.asyncio
async def test_reservation_covers_the_settled_search_charge() -> None:
    """Up to ``max_uses`` searches, the settled search charge stays inside the hold."""
    from routstr.payment.cost_calculation import CostData, calculate_cost

    max_uses = 4
    body = _body(
        {"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}
    )
    reserved = await _reservation(body, SEARCH_SATS)
    model_obj = _model(SEARCH_SATS)
    model_obj.id = body["model"]
    model_obj.pricing = Pricing(prompt=0.0, completion=0.0)

    async def settle(searches: int) -> CostData:
        response = {
            "model": body["model"],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 1024,
                "server_tool_use": {"web_search_requests": searches},
            },
        }
        with (
            patch.object(settings, "fixed_pricing", False),
            patch.object(settings, "tolerance_percentage", 0),
            patch("routstr.proxy.get_model_instance", return_value=model_obj),
            patch(
                "routstr.payment.cost_calculation.sats_usd_price",
                return_value=5.0e-5,
            ),
        ):
            cost = await calculate_cost(response, reserved, model_obj=model_obj)
        assert type(cost) is CostData
        return cost

    with_searches = await settle(max_uses)
    without_searches = await settle(0)

    assert (
        with_searches.total_msats - without_searches.total_msats
        == max_uses * SEARCH_SATS * 1000
    )
    assert with_searches.total_msats <= reserved


def test_default_respects_setting() -> None:
    body = _body({"type": "web_search_20250305", "name": "web_search"})

    with patch.object(settings, "web_search_default_max_uses", 2):
        assert reserved_web_searches(body) == 2
    with patch.object(settings, "web_search_default_max_uses", 0):
        assert reserved_web_searches(body) == 0
