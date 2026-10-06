"""The discounted reservation must still cover the request it admits.

Pricing below is PPQ's live ``gpt-6.1-sol`` rate (fetched 2026-10-06): a
1.05M-token context with no published completion limit.
"""

import math
from unittest.mock import patch

import pytest

from routstr.core.settings import settings
from routstr.payment.helpers import (
    calculate_discounted_max_cost,
    estimate_prompt_tokens,
)
from routstr.payment.models import (
    Architecture,
    Model,
    Pricing,
    TopProvider,
    _calculate_usd_max_costs,
    _update_model_sats_pricing,
)

SATS_TO_USD = 0.0006
PROMPT_USD = 2.11e-6
COMPLETION_USD = 10.55e-6


def _model(
    *,
    context_length: int = 1_050_000,
    top_provider: TopProvider | None = None,
) -> Model:
    model = Model(
        id="gpt-6.1-sol",
        name="gpt-6.1-sol",
        created=0,
        description="",
        context_length=context_length,
        architecture=Architecture(
            modality="text->text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="Unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=PROMPT_USD, completion=COMPLETION_USD),
        top_provider=top_provider,
    )
    max_prompt, max_completion, max_cost = _calculate_usd_max_costs(model)
    model.pricing.max_prompt_cost = max_prompt
    model.pricing.max_completion_cost = max_completion
    model.pricing.max_cost = max_cost
    return _update_model_sats_pricing(model, SATS_TO_USD)


def _body(max_tokens: int) -> dict:
    return {
        "model": "gpt-6.1-sol",
        "messages": [{"role": "user", "content": "Write a long story. " * 50}],
        "max_tokens": max_tokens,
    }


def _worst_case_msats(body: dict, max_tokens: int) -> int:
    prompt_tokens = estimate_prompt_tokens(body)
    return math.floor(
        (prompt_tokens * PROMPT_USD + max_tokens * COMPLETION_USD) / SATS_TO_USD * 1000
    )


async def _reserve(model: Model, body: dict) -> tuple[int, int]:
    assert model.sats_pricing is not None
    max_cost = int(model.sats_pricing.max_cost * 1000)
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1),
    ):
        return await calculate_discounted_max_cost(max_cost, body, model), max_cost


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(_model(), id="context_length_only"),
        pytest.param(
            _model(top_provider=TopProvider(context_length=1_050_000)),
            id="top_provider_context_only",
        ),
        pytest.param(
            _model(
                top_provider=TopProvider(
                    context_length=128_000, max_completion_tokens=128_000
                )
            ),
            id="completion_limit_equals_context",
        ),
        pytest.param(
            _model(top_provider=TopProvider(max_completion_tokens=128_000)),
            id="completion_limit_only",
        ),
        pytest.param(
            _model(
                top_provider=TopProvider(
                    context_length=1_050_000, max_completion_tokens=128_000
                )
            ),
            id="context_and_completion_limit",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reservation_covers_prompt_plus_requested_completion(
    model: Model,
) -> None:
    body = _body(100_000)
    reserved, max_cost = await _reserve(model, body)
    needed = min(max_cost, _worst_case_msats(body, 100_000))
    # Per-term flooring may leave the existing discount a msat or two above.
    assert needed <= reserved <= needed + 2


@pytest.mark.asyncio
async def test_reservation_never_exceeds_the_model_max_cost() -> None:
    model = _model(context_length=8_000)
    body = _body(1_000_000)
    reserved, max_cost = await _reserve(model, body)
    assert reserved == max_cost


@pytest.mark.asyncio
async def test_reservation_without_completion_cap_is_unchanged() -> None:
    model = _model()
    body = _body(0)
    del body["max_tokens"]
    reserved, max_cost = await _reserve(model, body)
    assert model.sats_pricing is not None
    prompt_tokens = estimate_prompt_tokens(body)
    prompt_delta = (1_050_000 - prompt_tokens) * model.sats_pricing.prompt
    assert reserved == max_cost - math.floor(prompt_delta * 1000)
