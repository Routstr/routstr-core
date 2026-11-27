"""The up-front reservation covers every completion ``n`` / ``best_of`` asks for.

The upstream generates and bills one completion per choice, each up to the
completion cap. A reservation sized for a single completion lets a low balance
key or a small X-Cashu token run n completions whose overrun is written off.
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
    completion_choice_count,
    generated_completion_count,
)
from routstr.payment.models import Pricing  # noqa: E402

COMPLETION_SATS = 0.002
MAX_COMPLETION_SATS = 300.0
MAX_COST_MSATS = 500_000


def _model() -> Mock:
    model_obj = Mock()
    model_obj.sats_pricing = Pricing(
        prompt=0.001,
        completion=COMPLETION_SATS,
        max_prompt_cost=200.0,
        max_completion_cost=MAX_COMPLETION_SATS,
        max_cost=MAX_COST_MSATS / 1000,
    )
    model_obj.top_provider = None
    model_obj.context_length = None
    model_obj.usage_multipliers = None
    return model_obj


def _body(**fields: Any) -> dict[str, Any]:
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Write a haiku."}],
        **fields,
    }


async def _reservation(body: dict[str, Any], path: str = "chat/completions") -> int:
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1),
    ):
        return await calculate_discounted_max_cost(
            MAX_COST_MSATS, body, _model(), path=path
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["n", "best_of"])
async def test_each_extra_choice_reserves_one_capped_completion(field: str) -> None:
    single = await _reservation(_body(max_tokens=1000, **{field: 1}))
    multi = await _reservation(_body(max_tokens=1000, **{field: 8}))
    assert multi - single == 7 * 1000 * COMPLETION_SATS * 1000


@pytest.mark.asyncio
async def test_uncapped_choices_reserve_the_full_completion_ceiling() -> None:
    single = await _reservation(_body(n=1))
    multi = await _reservation(_body(n=3))
    assert multi - single == 2 * MAX_COMPLETION_SATS * 1000


@pytest.mark.asyncio
async def test_reservation_covers_n_capped_completions() -> None:
    reserved = await _reservation(_body(max_tokens=1000, n=4))
    assert reserved >= 4 * 1000 * COMPLETION_SATS * 1000


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, 1),
        ({"n": 1}, 1),
        ({"n": 4}, 4),
        ({"best_of": 5, "n": 2}, 5),
        ({"n": "3"}, 3),
        ({"n": 0}, 1),
        ({"n": -2}, 1),
        ({"n": "many"}, 1),
        ({"n": None, "best_of": [2]}, 1),
    ],
)
def test_completion_choice_count(fields: dict[str, Any], expected: int) -> None:
    assert completion_choice_count(fields) == expected


@pytest.mark.asyncio
async def test_legacy_completion_batch_reserves_each_prompt_choice() -> None:
    body = {
        "model": "gpt-4o",
        "prompt": ["one", "two", "three"],
        "max_tokens": 1000,
        "n": 4,
    }
    reserved = await _reservation(body, path="v1/completions")

    assert generated_completion_count(body, "v1/completions") == 12
    assert reserved >= 12 * 1000 * COMPLETION_SATS * 1000


@pytest.mark.asyncio
async def test_legacy_token_id_prompt_is_not_treated_as_a_batch() -> None:
    body = {
        "model": "gpt-4o",
        "prompt": [101, 102, 103],
        "max_tokens": 1000,
        "n": 4,
    }
    single = await _reservation({**body, "n": 1}, path="completions")
    multi = await _reservation(body, path="completions")

    assert generated_completion_count(body, "completions") == 4
    assert multi - single == 3 * 1000 * COMPLETION_SATS * 1000


def test_prompt_batching_does_not_affect_other_completion_endpoints() -> None:
    body = {"prompt": ["one", "two", "three"], "n": 4}

    assert generated_completion_count(body, "chat/completions") == 4
    assert generated_completion_count(body, "responses") == 4
    assert generated_completion_count(body, "messages") == 4
