"""Additive 402 contract and reservation audit (no repricing)."""

import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from routstr.auth import (
    _validate_existing_cashu_key,
    pay_for_request,
    validate_bearer_key,
)
from routstr.core.db import ApiKey
from routstr.core.errors import build_payment_required_error
from routstr.core.exceptions import http_exception_handler
from routstr.core.settings import settings
from routstr.payment.helpers import calculate_discounted_max_cost, check_token_balance
from routstr.payment.models import (
    Architecture,
    Model,
    Pricing,
    TopProvider,
    _calculate_usd_max_costs,
)
from routstr.payment.pricing_trail import build_pricing_trail


def _model(cl=100, mct=20, context=100):
    return Model(
        id="m",
        name="m",
        created=0,
        description="",
        context_length=context,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="test",
            instruct_type=None,
        ),
        top_provider=TopProvider(context_length=cl, max_completion_tokens=mct),
        pricing=Pricing(prompt=0.01, completion=0.02),
        sats_pricing=Pricing(
            prompt=1,
            completion=2,
            max_prompt_cost=100,
            max_completion_cost=40,
            max_cost=120,
        ),
    )


def test_serializer():
    d = build_payment_required_error(
        code="insufficient_balance",
        message="balance",
        required_msats=28989,
        balance_msats=25000,
        reserved_msats=4000,
        model="m",
    )["error"]["details"]
    assert (d["required_msats"], d["available_msats"], d["shortfall_msats"]) == (
        28989,
        21000,
        7989,
    )
    assert d["unit"] == "msat"
    assert d["reserved_msats"] == 4000
    assert d["pricing"]["formula"] == "unavailable"
    d = build_payment_required_error(
        code="insufficient_balance",
        message="balance",
        required_msats=1000,
        balance_msats=10,
        reserved_msats=20,
    )["error"]["details"]
    assert d["available_msats"] == 0
    assert d["shortfall_msats"] == 1000


@pytest.mark.parametrize(
    "cl,mct,context,formula",
    [
        (100, 20, 100, "context_plus_completion"),
        (20, 100, 100, "context_le_completion"),
        (100, None, 100, "context_only"),
        (None, 20, 100, "completion_only"),
        (None, None, 100, "model_context_only"),
        (None, None, 0, "flat_estimate"),
    ],
)
def test_pricing_formulas(monkeypatch, cl, mct, context, formula):
    monkeypatch.setattr(settings, "fixed_pricing", False)
    m = _model(cl, mct, context)
    trail = build_pricing_trail(m, 114000)
    assert trail["formula"] == trail["max_cost_formula"] == formula
    p, c, total = _calculate_usd_max_costs(m)
    assert trail["max_cost_computed"]["max_cost_usd"] == total
    assert trail["max_cost_computed"]["max_prompt_cost_usd"] == p
    assert trail["max_cost_computed"]["max_completion_cost_usd"] == c
    assert trail["max_cost_computed"]["max_cost_msats"] == 120000
    assert (
        trail["max_cost_computed"]["exchange_rate_source"]
        == "not_recorded_in_model_cache"
    )
    if formula == "flat_estimate":
        assert trail["max_cost_inputs"]["coefficients"] == dict(
            prompt=1000000,
            completion=32000,
            request=100000,
            image=100,
            web_search=1000,
            internal_reasoning=100,
        )


def test_fixed_fallback_and_invalid_pricing(monkeypatch):
    monkeypatch.setattr(settings, "fixed_pricing", True)
    monkeypatch.setattr(settings, "fixed_cost_per_request", 1)
    monkeypatch.setattr(settings, "min_request_msat", 2000)
    assert build_pricing_trail(None, 2000)["base_source"] == "fixed_pricing"
    assert build_pricing_trail(None, 2000)["min_request_applied"] is True
    monkeypatch.setattr(settings, "fixed_pricing", False)
    assert build_pricing_trail(None, 2000)["base_source"] == "fallback_fixed"
    m = _model()
    m.pricing.prompt = float("nan")
    assert build_pricing_trail(m, 2000)["formula"] == "unavailable"


async def test_discount_trace_does_not_change_cost(monkeypatch):
    monkeypatch.setattr(settings, "fixed_pricing", False)
    monkeypatch.setattr(settings, "min_request_msat", 1000)
    monkeypatch.setattr(settings, "tolerance_percentage", 5)
    m = _model()
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 2,
        "max_completion_tokens": 3,
    }
    trail = build_pricing_trail(m, 114000)
    expected = await calculate_discounted_max_cost(114000, body, m)
    result = await calculate_discounted_max_cost(114000, body, m, pricing_trail=trail)
    assert expected == result == trail["required_msats"]
    d = trail["discounts"]
    assert d["completion_cap_used"] == 3
    assert result == max(
        1000, 114000 - d["prompt_delta_msats"] - d["completion_delta_msats"]
    )


async def test_image_trace_uses_actual_estimate_once(monkeypatch):
    monkeypatch.setattr(settings, "fixed_pricing", False)
    trail = build_pricing_trail(_model(), 100000)
    with patch(
        "routstr.payment.helpers.image_reservation_msats", return_value=12345
    ) as image:
        result = await calculate_discounted_max_cost(
            100000, {}, _model(), pricing_trail=trail
        )
    image.assert_called_once()
    assert result == trail["required_msats"] == 12345
    assert trail["discounts"]["image_reservation_msats"] == 12345


@pytest.mark.parametrize(
    "producer", ["cashu", "bearer", "pre_reservation", "concurrent", "x_cashu"]
)
async def test_all_producers_mirror_contract(monkeypatch, producer):
    monkeypatch.setattr(settings, "min_request_msat", 1000)
    key = ApiKey(hashed_key="test", balance=25000, reserved_balance=4000)
    session = AsyncMock()
    session.get.return_value = key
    if producer == "concurrent":
        # Initial snapshot admits; UPDATE loses the race, refresh sees depletion.
        key.balance = 50000
        session.exec.return_value = Mock(rowcount=0)

        async def refresh(k):
            k.balance = 25000

        session.refresh.side_effect = refresh
    with pytest.raises(HTTPException) as raised:
        if producer == "cashu":
            _validate_existing_cashu_key(key, None, None, 28989)
        elif producer == "bearer":
            await validate_bearer_key("sk-test", session, min_cost=28989)
        elif producer in ("pre_reservation", "concurrent"):
            await pay_for_request(key, 28989, session)
        else:
            with (
                patch(
                    "routstr.payment.helpers.deserialize_token_from_string",
                    return_value=Mock(amount=21, unit="sat", mint="mint"),
                ),
                patch(
                    "routstr.payment.helpers.is_trusted_source_mint", return_value=True
                ),
            ):
                check_token_balance({"x-cashu": "cashu-token"}, {"model": "m"}, 28989)
    request = Request({"type": "http", "path": "/v1/chat/completions", "headers": []})
    request.state.request_id = "req-test"
    response = await http_exception_handler(request, raised.value)
    body = json.loads(response.body)
    assert response.status_code == 402
    assert body["error"] == body["detail"]["error"]
    assert body["request_id"] == "req-test"
    error = body["error"]
    assert error["type"] == "insufficient_quota"
    assert isinstance(error["message"], str)
    d = error["details"]
    assert (d["required_msats"], d["available_msats"], d["shortfall_msats"]) == (
        28989,
        21000,
        7989,
    )
    assert d["unit"] == "msat"
    assert isinstance(d["pricing"]["formula"], str)
    if producer == "x_cashu":
        assert error["code"] == "minimum_balance_required"
        assert body["detail"]["type"] == "minimum_balance_required"
        assert body["detail"]["amount_required_msat"] == 28989
        assert body["detail"]["reason"] == "Insufficient balance"
        assert "reason" not in body
    else:
        assert error["code"] == "insufficient_balance"
        assert d["reserved_msats"] == 4000
    if producer == "concurrent":
        assert error["available_balance"] == 21000


async def test_proxy_auth_handoff_retains_actual_request_pricing():
    from routstr.proxy import get_bearer_token_key

    key = ApiKey(hashed_key="test", balance=21000)
    session = AsyncMock()
    session.get.return_value = key
    pricing = {"formula": "context_only", "required_msats": 28989}
    with pytest.raises(HTTPException) as raised:
        await get_bearer_token_key(
            {},
            "v1/chat/completions",
            session,
            "Bearer sk-test",
            28989,
            "m",
            pricing=pricing,
        )
    error = raised.value.detail["error"]
    assert error["code"] == "insufficient_balance"
    assert "for this model" in error["message"]
    assert error["details"]["model"] == "m"
    assert error["details"]["pricing"] is pricing
    assert error["details"]["shortfall_msats"] == 7989


async def test_image_override_resets_base_minimum_flag(monkeypatch):
    monkeypatch.setattr(settings, "fixed_pricing", False)
    monkeypatch.setattr(settings, "min_request_msat", 1000)
    m = _model()
    m.sats_pricing.max_cost = 0.1
    trail = build_pricing_trail(m, 1000)
    assert trail["min_request_applied"] is True
    with patch("routstr.payment.helpers.image_reservation_msats", return_value=12345):
        result = await calculate_discounted_max_cost(1000, {}, m, pricing_trail=trail)
    assert result == 12345
    assert trail["min_request_applied"] is False


async def test_proxy_passes_calculated_trail_into_early_balance_rejection(monkeypatch):
    from routstr import proxy as proxy_module

    from .proxy_test_utils import mock_request_stream, patch_proxy_session

    monkeypatch.setattr(settings, "fixed_pricing", False)
    monkeypatch.setattr(settings, "min_request_msat", 1000)
    request = MagicMock()
    request.method = "POST"
    request.headers = {"authorization": "Bearer sk-test"}
    request.state = MagicMock()
    request.state.request_id = "req-test"
    mock_request_stream(request, b'{"model":"m","messages":[]}')
    session = AsyncMock()
    session.get.return_value = ApiKey(hashed_key="test", balance=0)
    upstream = Mock()
    upstream.db_id = 1
    upstream.provider_type = "test"
    upstream.base_url = "http://localhost"
    model = _model()
    with (
        patch.object(proxy_module, "get_candidates", return_value=[(model, upstream)]),
        patch_proxy_session(session),
    ):
        with pytest.raises(HTTPException) as raised:
            await proxy_module.proxy(request, "v1/chat/completions")
    error = raised.value.detail["error"]
    assert raised.value.status_code == 402
    assert error["details"]["model"] == "m"
    pricing = error["details"]["pricing"]
    assert pricing["formula"] == "context_plus_completion"
    assert pricing["required_msats"] == error["details"]["required_msats"]
    assert "prompt_tokens_estimated" in pricing["discounts"]
