"""OpenRouter image requests are quoted on one endpoint and pinned to it.

OpenRouter serves a model from several endpoints, each with its own prices
and accepted parameters. A blended book can reserve on one price while the
router bills another, so the request is validated against and priced on the
endpoint it will be sent to, ``provider.only`` names that endpoint, and
client routing is refused.
"""

from __future__ import annotations

import json
import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routstr import proxy as proxy_module
from routstr.auth import ReservationSnapshot
from routstr.core.db import ApiKey
from routstr.core.settings import settings
from routstr.payment.image_pricing import (
    ImageRequestRefused,
    image_reservation_msats,
    quote_image_endpoint,
)
from routstr.payment.models import Architecture, Model, Pricing
from routstr.upstream.image_catalog import (
    attach_image_books,
    openrouter_book_from_endpoints,
)
from routstr.upstream.openrouter import OpenRouterUpstreamProvider

# 1 USD of upstream cost is 1,000 sats on these fixtures.
SATS_PER_USD = 1_000.0
N_RANGE = {"type": "range", "min": 1, "max": 4}


def _endpoint(
    tag: str | None,
    *lines: dict[str, Any],
    supported: dict[str, Any] | None = None,
) -> dict[str, Any]:
    endpoint: dict[str, Any] = {
        "pricing": list(lines),
        "supported_parameters": {"n": N_RANGE} if supported is None else supported,
    }
    if tag is not None:
        endpoint["provider_tag"] = tag
    return endpoint


def _per_image(usd: float, variant: str | None = None) -> dict[str, Any]:
    return {
        "billable": "output_image",
        "unit": "image",
        "cost_usd": usd,
        "variant": variant,
    }


def _model(*endpoints: dict[str, Any]) -> Model:
    book = openrouter_book_from_endpoints({"endpoints": list(endpoints)}, "img")
    assert book is not None
    model = Model(
        id="img",
        name="img",
        created=0,
        description="",
        context_length=0,
        architecture=Architecture(
            modality="text->image",
            input_modalities=["text"],
            output_modalities=["image"],
            tokenizer="Unknown",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=0.0, completion=0.0),
    )
    [priced] = attach_image_books([model], {"img": book}, source="test")
    sats = priced.pricing.copy(
        update={
            "image_output": priced.pricing.image_output * SATS_PER_USD,
            "image": priced.pricing.image * SATS_PER_USD,
        }
    )
    return priced.copy(update={"sats_pricing": sats})


def _msats(usd: float) -> int:
    return math.ceil(usd * SATS_PER_USD * 1000)


def _tag(body: dict, model: Model) -> str | None:
    book = quote_image_endpoint(body, model).image_pricing
    assert book is not None
    return book.endpoint_tag


def _refusal(body: dict, model: Model) -> str:
    with pytest.raises(ImageRequestRefused) as refused:
        quote_image_endpoint(body, model)
    return str(refused.value)


def test_each_uniquely_tagged_endpoint_gets_its_own_book() -> None:
    book = openrouter_book_from_endpoints(
        {
            "endpoints": [
                _endpoint(
                    "a", _per_image(0.04), supported={"seed": {"type": "boolean"}}
                ),
                _endpoint("b", _per_image(0.02)),
                _endpoint(None, _per_image(0.01)),
            ]
        },
        "img",
    )
    assert book is not None
    assert book.max_usd == pytest.approx(0.04)
    assert set(book.endpoints) == {"a", "b"}
    assert book.endpoints["a"].endpoint_tag == "a"
    assert book.endpoints["a"].parameters == {"seed": {"type": "boolean"}}
    assert book.endpoints["b"].max_usd == pytest.approx(0.02)
    assert book.endpoint_tag is None


def test_a_tag_listed_twice_is_not_quotable() -> None:
    """The tag pins a provider, not a record: which price applies is unknown."""
    book = openrouter_book_from_endpoints(
        {
            "endpoints": [
                _endpoint("dup", _per_image(0.04)),
                _endpoint("dup", _per_image(0.01)),
                _endpoint("ok", _per_image(0.03)),
            ]
        },
        "img",
    )
    assert book is not None
    assert set(book.endpoints) == {"ok"}
    only_dup = {
        "endpoints": [
            _endpoint("dup", _per_image(0.04)),
            _endpoint("dup", _per_image(0.01)),
        ]
    }
    assert openrouter_book_from_endpoints(only_dup, "img") is None


def test_an_endpoint_without_a_bounded_price_is_not_quoted() -> None:
    model = _model(
        _endpoint(
            "tokens", {"billable": "output_image", "unit": "token", "cost_usd": 1}
        ),
        _endpoint("flat", _per_image(0.04)),
    )
    assert model.image_pricing is not None
    assert set(model.image_pricing.endpoints) == {"flat"}


def test_the_request_is_quoted_on_the_cheapest_endpoint() -> None:
    model = _model(
        _endpoint("dear", _per_image(0.04)),
        _endpoint("cheap", _per_image(0.02)),
    )
    quoted = quote_image_endpoint({"prompt": "x"}, model)
    assert quoted.image_pricing is not None
    assert quoted.image_pricing.endpoint_tag == "cheap"
    assert image_reservation_msats({"prompt": "x"}, quoted) == _msats(0.02)
    # Same sats per USD as the blended model it came from.
    assert quoted.sats_pricing is not None
    assert quoted.sats_pricing.image_output == pytest.approx(0.02 * SATS_PER_USD)


def test_reference_surcharges_come_from_the_quoted_endpoint() -> None:
    references = {"type": "range", "min": 0, "max": 4}
    model = _model(
        _endpoint(
            "edits",
            _per_image(0.03),
            {"billable": "input_image", "unit": "image", "cost_usd": 0.003},
            supported={"n": N_RANGE, "input_references": references},
        )
    )
    ref = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
    body = {"prompt": "x", "n": 2, "input_references": [ref, ref]}
    quoted = quote_image_endpoint(body, model)
    assert image_reservation_msats(body, quoted) == _msats(2 * 0.03 + 2 * 0.003)


def test_an_endpoint_that_does_not_offer_the_parameter_is_skipped() -> None:
    model = _model(
        _endpoint(
            "small",
            _per_image(0.01),
            supported={"resolution": {"type": "enum", "values": ["1K"]}},
        ),
        _endpoint(
            "large",
            _per_image(0.05),
            supported={"resolution": {"type": "enum", "values": ["1K", "2K"]}},
        ),
    )
    assert _tag({"prompt": "x", "resolution": "2K"}, model) == "large"
    assert _tag({"prompt": "x", "resolution": "1K"}, model) == "small"
    # ``size`` tier shorthand is the endpoint's ``resolution``.
    assert _tag({"prompt": "x", "size": "2K"}, model) == "large"
    assert "Unsupported resolution" in _refusal(
        {"prompt": "x", "resolution": "4K"}, model
    )


def test_endpoint_ranges_are_enforced() -> None:
    model = _model(
        _endpoint(
            "few",
            _per_image(0.01),
            supported={"n": {"type": "range", "min": 1, "max": 2}},
        ),
        _endpoint(
            "many",
            _per_image(0.03),
            supported={"n": {"type": "range", "min": 1, "max": 6}},
        ),
    )
    assert _tag({"prompt": "x", "n": 2}, model) == "few"
    assert _tag({"prompt": "x", "n": 3}, model) == "many"
    assert "outside endpoint limits" in _refusal({"prompt": "x", "n": 7}, model)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"prompt": " "}, "prompt must be a nonempty string"),
        ({"prompt": "x", "provider": {"order": ["a"]}}, "Client provider routing"),
        ({"prompt": "x", "steps": 50}, "Unsupported image request fields: steps"),
        ({"prompt": "x", "n": 11}, "n must be an integer"),
        ({"prompt": "x", "n": True}, "n must be an integer"),
        ({"prompt": "x", "user": "u" * 257}, "user must be a string"),
        ({"prompt": "x", "output_format": "gif"}, "Unsupported output_format"),
        (
            {"prompt": "x", "background": "transparent", "output_format": "jpeg"},
            "Transparent background requires png or webp",
        ),
        ({"prompt": "x", "response_format": "html"}, "Unsupported response_format"),
        ({"prompt": "x", "size": "huge"}, "Unsupported size"),
        ({"prompt": "x", "size": "2K", "resolution": "1K"}, "size conflicts"),
        ({"prompt": "x", "input_references": ["data:x"]}, "image_url content parts"),
        (
            {
                "prompt": "x",
                "input_references": [
                    {"type": "image_url", "image_url": {"url": "ftp://x/a.png"}}
                ],
            },
            "HTTP(S) or base64 image data URLs",
        ),
        ({"prompt": "x", "seed": 7}, "Endpoint does not support seed"),
        ({"prompt": "x", "quality": "high"}, "Endpoint does not support quality"),
    ],
)
def test_requests_the_quote_cannot_account_for_are_refused(
    body: dict, reason: str
) -> None:
    model = _model(_endpoint("a", _per_image(0.02)))
    assert reason in _refusal(body, model)


def test_openai_compatible_fields_are_accepted() -> None:
    model = _model(_endpoint("a", _per_image(0.02)))
    body = {
        "model": "img",
        "prompt": "x",
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
        "user": "u",
    }
    assert _tag(body, model) == "a"


def test_seed_needs_an_integer_where_the_endpoint_supports_it() -> None:
    model = _model(
        _endpoint("a", _per_image(0.02), supported={"seed": {"type": "boolean"}})
    )
    assert _tag({"prompt": "x", "seed": 7}, model) == "a"
    assert "seed must be an integer" in _refusal({"prompt": "x", "seed": True}, model)


def test_a_model_with_no_endpoint_to_pin_is_refused() -> None:
    model = _model(_endpoint("a", _per_image(0.02)))
    assert model.image_pricing is not None
    blank = model.copy(
        update={"image_pricing": model.image_pricing.copy(update={"endpoints": {}})}
    )
    assert "No image endpoint" in _refusal({"prompt": "x"}, blank)


def test_the_quoted_endpoint_is_pinned_and_client_routing_dropped() -> None:
    model = _model(_endpoint("cheap", _per_image(0.02)))
    quoted = quote_image_endpoint({"prompt": "x"}, model)
    provider = OpenRouterUpstreamProvider(api_key="k")
    body = json.dumps(
        {"model": "img", "prompt": "x", "provider": {"order": ["other"]}}
    ).encode()

    out = provider.prepare_request_body(body, quoted)

    assert out is not None
    assert json.loads(out)["provider"] == {"only": ["cheap"], "allow_fallbacks": False}


async def _run_proxy(path: str, body: dict, model: Model) -> tuple[Any, AsyncMock]:
    upstream = MagicMock()
    upstream.provider_type = "openrouter"
    upstream.base_url = "https://openrouter.ai/api/v1"
    upstream.provider_fee = 1.0
    upstream.db_id = None
    upstream.prepare_headers = MagicMock(side_effect=lambda h: h)
    upstream.forward_request = AsyncMock(return_value=MagicMock(status_code=200))
    key = ApiKey(hashed_key="imagekey", balance=10_000_000)
    reservation = ReservationSnapshot(
        release_id="release",
        key_hash=key.hashed_key,
        billing_key_hash=key.hashed_key,
        reserved_msats=20_000,
    )
    request = MagicMock()
    request.method = "POST"
    request.headers = {"authorization": "Bearer sk-key"}
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    request.state = MagicMock()
    request.state.request_id = "req-1"
    with (
        patch.object(proxy_module, "get_candidates", return_value=[(model, upstream)]),
        patch.object(
            proxy_module, "get_max_cost_for_model", AsyncMock(return_value=20_000)
        ),
        patch.object(
            proxy_module,
            "calculate_discounted_max_cost",
            AsyncMock(return_value=20_000),
        ),
        patch.object(proxy_module, "check_token_balance", MagicMock()),
        patch.object(proxy_module, "get_bearer_token_key", AsyncMock(return_value=key)),
        patch.object(
            proxy_module, "pay_for_request", AsyncMock(return_value=reservation)
        ),
        patch.object(proxy_module, "revert_pay_for_request", AsyncMock()),
        patch.object(proxy_module, "sats_usd_price", MagicMock(return_value=0.001)),
    ):
        response = await proxy_module._proxy(
            request, path, MagicMock(), await request.body()
        )
    return response, upstream.forward_request


def _body(response: Any) -> dict:
    return json.loads(bytes(response.body))


@pytest.mark.asyncio
async def test_proxy_forwards_the_model_quoted_on_one_endpoint() -> None:
    model = _model(
        _endpoint("dear", _per_image(0.04)),
        _endpoint("cheap", _per_image(0.02)),
    )
    response, forward = await _run_proxy(
        "v1/images/generations", {"model": "img", "prompt": "x"}, model
    )

    assert response.status_code == 200
    forward.assert_awaited_once()
    assert forward.await_args is not None
    forwarded_model = forward.await_args.args[7]
    assert forwarded_model.image_pricing.endpoint_tag == "cheap"


@pytest.mark.asyncio
async def test_proxy_reports_why_the_request_was_refused() -> None:
    model = _model(_endpoint("cheap", _per_image(0.02)))
    response, forward = await _run_proxy(
        "v1/images/generations",
        {"model": "img", "prompt": "x", "provider": {"order": ["cheap"]}},
        model,
    )

    assert response.status_code == 400
    assert "Client provider routing" in json.dumps(_body(response))
    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_refuses_a_quote_above_the_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.02 USD at 1,000 sats per USD is 20 sats, 0.02 USD at the node's rate."""
    model = _model(_endpoint("cheap", _per_image(0.02)))
    body = {"model": "img", "prompt": "x"}

    monkeypatch.setattr(settings, "image_max_request_usd", 0.019)
    response, forward = await _run_proxy("v1/images/generations", body, model)
    assert response.status_code == 400
    assert "per-request budget" in json.dumps(_body(response))
    forward.assert_not_awaited()

    monkeypatch.setattr(settings, "image_max_request_usd", 0.021)
    response, forward = await _run_proxy("v1/images/generations", body, model)
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["v1/chat/completions", "v1/responses"])
async def test_an_image_only_model_is_refused_on_text_routes(path: str) -> None:
    """It has no token price, so a text route would serve it free."""
    model = _model(_endpoint("cheap", _per_image(0.02)))
    response, forward = await _run_proxy(
        path, {"model": "img", "messages": [{"role": "user", "content": "x"}]}, model
    )

    assert response.status_code == 400
    assert "requires the images API" in json.dumps(_body(response))
    forward.assert_not_awaited()
