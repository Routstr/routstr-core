"""Request-specific reservations can reverse the static candidate order."""

import json
import os
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from PIL import Image

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

from routstr import proxy as proxy_module  # noqa: E402
from routstr.core.settings import settings  # noqa: E402
from routstr.payment import helpers as payment_helpers  # noqa: E402
from routstr.payment.models import Architecture, Model, Pricing  # noqa: E402

from .proxy_test_utils import mock_request_stream, patch_proxy_session  # noqa: E402

MODEL_ID = "cross-over-model"


def _model(name: str, max_prompt: float, max_completion: float) -> Model:
    model = Model(
        id=MODEL_ID,
        name=name,
        created=1,
        description="",
        context_length=1,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="test",
            instruct_type=None,
        ),
        pricing=Pricing(prompt=max_prompt, completion=max_completion),
    )
    model.sats_pricing = Pricing(
        prompt=max_prompt,
        completion=1,
        max_prompt_cost=max_prompt,
        max_completion_cost=max_completion,
        max_cost=max_prompt + max_completion,
    )
    return model


def _upstream(name: str) -> MagicMock:
    upstream = MagicMock()
    upstream.provider_type = name
    upstream.base_url = f"https://{name}.example/v1"
    upstream.db_id = 1 if name == "candidate-a" else 2
    upstream.supports_ehbp = False
    upstream.handle_x_cashu = AsyncMock(return_value=Response("ok", status_code=200))
    return upstream


@pytest.mark.parametrize(
    ("token_balance_msats", "expected_upstream"),
    [(30_000, "candidate-b"), (100_000, "candidate-a")],
)
@pytest.mark.asyncio
async def test_balance_gate_uses_first_affordable_n8_candidate(
    token_balance_msats: int,
    expected_upstream: str,
) -> None:
    # A has the smaller one-completion ceiling (11 < 12 sats), but its large
    # completion allowance makes n=8 cost 81 sats versus B's 26 sats.
    candidate_a = _model("candidate-a", max_prompt=1, max_completion=10)
    candidate_b = _model("candidate-b", max_prompt=10, max_completion=2)
    upstream_a = _upstream("candidate-a")
    upstream_b = _upstream("candidate-b")
    body = json.dumps({"model": MODEL_ID, "prompt": "hi", "n": 8}).encode()
    request = MagicMock()
    request.method = "POST"
    request.headers = {"content-type": "application/json", "x-cashu": "token"}
    request.state = MagicMock()
    request.state.request_id = "request-specific-reservation"
    mock_request_stream(request, body)

    checked_reservations: list[int] = []

    def balance_gate(
        _headers: dict,
        _body: dict,
        reservation: int,
        *,
        pricing: dict | None = None,
    ) -> int:
        del pricing
        checked_reservations.append(reservation)
        if reservation > token_balance_msats:
            raise HTTPException(status_code=402, detail="Insufficient balance")
        return token_balance_msats

    with (
        patch.object(
            proxy_module,
            "get_candidates",
            return_value=[(candidate_a, upstream_a), (candidate_b, upstream_b)],
        ),
        patch.object(proxy_module, "check_token_balance", side_effect=balance_gate),
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1),
        patch_proxy_session(MagicMock()),
    ):
        response = await proxy_module.proxy(request, "v1/completions")

    assert response.status_code == 200
    assert checked_reservations == [26_000]
    if expected_upstream == "candidate-a":
        upstream_a.handle_x_cashu.assert_awaited_once()
        upstream_b.handle_x_cashu.assert_not_awaited()
    else:
        upstream_b.handle_x_cashu.assert_awaited_once()
        upstream_a.handle_x_cashu.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_image_estimate_is_shared_across_candidate_reservations() -> None:
    candidate_a = _model("candidate-a", max_prompt=1, max_completion=10)
    candidate_b = _model("candidate-b", max_prompt=10, max_completion=2)
    upstream_a = _upstream("candidate-a")
    upstream_b = _upstream("candidate-b")
    image_urls = [f"https://images.example/{index}.png" for index in range(10)]
    body = json.dumps(
        {
            "model": MODEL_ID,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": url}}
                        for url in image_urls
                    ],
                }
            ],
        }
    ).encode()
    request = MagicMock()
    request.method = "POST"
    request.headers = {"content-type": "application/json", "x-cashu": "token"}
    request.state = MagicMock()
    request.state.request_id = "shared-image-estimate"
    mock_request_stream(request, body)

    image = Image.new("RGB", (512, 512), color="red")
    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")
    fetch_image = AsyncMock(return_value=image_buffer.getvalue())
    reservations: dict[str, tuple[int, int]] = {}
    calculate_reservation = payment_helpers.calculate_discounted_max_cost

    async def record_reservation(*args: Any, **kwargs: Any) -> int:
        reservation = await calculate_reservation(*args, **kwargs)
        model_obj = kwargs["model_obj"]
        pricing_trail = kwargs["pricing_trail"]
        reservations[model_obj.name] = (
            reservation,
            pricing_trail["discounts"]["image_tokens_estimated"],
        )
        return reservation

    with (
        patch.object(
            proxy_module,
            "get_candidates",
            return_value=[(candidate_a, upstream_a), (candidate_b, upstream_b)],
        ),
        patch.object(proxy_module, "calculate_discounted_max_cost", record_reservation),
        patch.object(proxy_module, "check_token_balance", return_value=100_000),
        patch.object(payment_helpers, "_fetch_image_from_url", fetch_image),
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0),
        patch.object(settings, "min_request_msat", 1),
        patch_proxy_session(MagicMock()),
    ):
        response = await proxy_module.proxy(request, "v1/chat/completions")

    assert response.status_code == 200
    assert fetch_image.await_count == payment_helpers.IMAGE_FETCH_MAX_PER_REQUEST
    assert reservations == {
        "candidate-a": (11_000, 3_570),
        "candidate-b": (12_000, 3_570),
    }
    upstream_a.handle_x_cashu.assert_awaited_once()
    upstream_b.handle_x_cashu.assert_not_awaited()
