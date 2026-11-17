"""API-key settlement under flat fixed pricing charges exactly the hold.

With ``fixed_pricing`` on and no per-1k rate, the reservation is the flat
``fixed_cost_per_request``; settlement must not fall through to the served
model's token prices, which used to bill a large completion far past the hold.
"""

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import ApiKey
from routstr.core.settings import settings
from routstr.payment.models import Architecture, Model, Pricing

FLAT_SATS = 7
FLAT_MSATS = FLAT_SATS * 1000
START_BALANCE = 10_000_000


def _priced_model() -> Model:
    return Model(
        id="token-priced-model",
        name="token-priced-model",
        created=0,
        description="",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="test",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.01, completion=0.01),
        sats_pricing=Pricing(prompt=0.01, completion=0.01),
        per_request_limits=None,
        top_provider=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_tokens", [2, 100_000])
async def test_api_key_settles_at_flat_fee(
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    completion_tokens: int,
) -> None:
    from routstr.auth import adjust_payment_for_tokens, pay_for_request
    from routstr.payment.helpers import get_max_cost_for_model

    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_cost_per_request", FLAT_SATS)
    monkeypatch.setattr(settings, "fixed_per_1k_input_tokens", 0)
    monkeypatch.setattr(settings, "fixed_per_1k_output_tokens", 0)

    model = _priced_model()
    hold = await get_max_cost_for_model(model.id, integration_session, model)
    assert hold == FLAT_MSATS

    key = ApiKey(
        hashed_key=f"test_{uuid.uuid4().hex}",
        balance=START_BALANCE,
        reserved_balance=0,
        total_spent=0,
        total_requests=0,
    )
    integration_session.add(key)
    await integration_session.commit()
    await pay_for_request(key, hold, integration_session)

    response_data = {
        "model": model.id,
        "usage": {"prompt_tokens": 3, "completion_tokens": completion_tokens},
    }
    cost = await adjust_payment_for_tokens(
        key, response_data, integration_session, hold, model, 1.0
    )

    await integration_session.refresh(key)
    assert cost["total_msats"] == FLAT_MSATS
    assert cost["charged_msats"] == FLAT_MSATS
    assert cost["output_tokens"] == completion_tokens
    assert key.balance == START_BALANCE - FLAT_MSATS
    assert key.reserved_balance == 0
    assert key.total_spent == FLAT_MSATS
