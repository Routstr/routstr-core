"""Images have their own quote and completed-result settlement contract."""

import base64
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.payment.cost_calculation import CostData
from routstr.payment.images import (
    ImageQuote,
    ImageRequestError,
    calculate_image_cost,
    quote_image_request,
)


def capability(rate: Any = 0.04, unit: str = "image") -> dict[str, Any]:
    return {
        "endpoints": [
            {
                "provider_tag": "seed",
                "provider_slug": "seed",
                "supported_parameters": {
                    "n": {"type": "range", "min": 1, "max": 10},
                    "resolution": {"type": "enum", "values": ["1K", "2K"]},
                    "seed": {"type": "boolean"},
                    "input_references": {"type": "range", "min": 0, "max": 14},
                },
                "pricing": [
                    {"billable": "output_image", "unit": unit, "cost_usd": rate}
                ],
            }
        ]
    }


def quote(
    body: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ImageQuote:
    return quote_image_request(
        body or {"model": "alias", "prompt": "A red panda"},
        upstream_model_id="seed/model",
        capabilities=capabilities or capability(),
        provider_fee=kwargs.get("provider_fee", 1.05),
        usd_per_sat=0.00005,
        max_request_usd=kwargs.get("max_request_usd", 1),
    )


def response(cost: Any = 0.04, count: int = 1) -> dict[str, Any]:
    return {
        "created": 1748372400,
        "data": [
            {"b64_json": base64.b64encode(b"image bytes").decode()}
            for _ in range(count)
        ],
        "usage": {"cost": cost},
    }


def test_quote_pins_provider_and_freezes_fx_and_fee() -> None:
    import json

    q = quote({"model": "alias", "prompt": "a panda", "n": 2})
    assert q.upstream_max_usd == Decimal("0.08")
    assert q.reserved_msats == 1680000
    assert q.usd_per_sat == Decimal("0.00005")
    assert q.provider_fee == Decimal("1.05")
    assert json.loads(q.body_json) == {
        "model": "seed/model",
        "prompt": "a panda",
        "n": 2,
        "stream": False,
        "provider": {"only": ["seed"], "allow_fallbacks": False},
    }
    with pytest.raises(Exception):
        cast(Any, q).provider_tag = "other"


def test_variants_reserve_maximum_not_sum() -> None:
    cap = capability()
    cap["endpoints"][0]["pricing"].append(
        {
            "billable": "output_image",
            "unit": "image",
            "cost_usd": 0.06,
            "variant": "high_2k",
        }
    )
    assert quote(capabilities=cap).upstream_max_usd == Decimal("0.06")


@pytest.mark.parametrize(
    "patch_body",
    [
        {"n": True},
        {"n": 0},
        {"n": 11},
        {"n": 1.1},
        {"prompt": []},
        {"model": None},
        {"stream": True},
        {"stream": 0},
        {"provider": {}},
        {"max_tokens": 100},
        {"output_format": []},
        {"quality": "high"},
        {"seed": True},
        {"size": "2048x2048"},
        {"resolution": "4K"},
        {"input_references": ["https://example.com/a.png"]},
        {"user": 1},
    ],
)
def test_invalid_request_fails_closed(patch_body: dict[str, Any]) -> None:
    with pytest.raises(ImageRequestError):
        quote({"model": "alias", "prompt": "panda", **patch_body})


@pytest.mark.parametrize("unit", ["token", "megapixel", "unknown"])
def test_unbounded_output_prices_rejected(unit: str) -> None:
    with pytest.raises(ImageRequestError, match="bound") as exc:
        quote(capabilities=capability(unit=unit))
    assert exc.value.code == "image_pricing_unbounded"


@pytest.mark.parametrize("rate", [True, -1, float("nan"), float("inf"), "bad"])
def test_invalid_rates_rejected(rate: Any) -> None:
    with pytest.raises(ImageRequestError):
        quote(capabilities=capability(rate=rate))


def test_token_input_price_cannot_hide_behind_fixed_output() -> None:
    cap = capability()
    cap["endpoints"][0]["pricing"].append(
        {"billable": "input_text", "unit": "token", "cost_usd": 0.00001}
    )
    with pytest.raises(ImageRequestError):
        quote(capabilities=cap)


def test_reference_content_parts_and_integer_seed() -> None:
    q = quote(
        {
            "model": "alias",
            "prompt": "panda",
            "seed": 12,
            "input_references": [
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/ref.png"},
                }
            ],
        }
    )
    assert q.upstream_max_usd == Decimal("0.04")


def test_reference_price_in_quote() -> None:
    cap = capability()
    cap["endpoints"][0]["pricing"].append(
        {"billable": "input_image", "unit": "image", "cost_usd": 0.01}
    )
    q = quote(
        {
            "model": "alias",
            "prompt": "panda",
            "input_references": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aQ=="},
                }
            ],
        },
        capabilities=cap,
    )
    assert q.upstream_max_usd == Decimal("0.05")


def test_budget_is_marked_up_admission_limit() -> None:
    with pytest.raises(ImageRequestError) as exc:
        quote(max_request_usd=0.041)
    assert exc.value.code == "image_request_budget_exceeded"


def test_completed_cost_only_response_is_billed_without_synthetic_tokens() -> None:
    cost = calculate_image_cost(response(), quote=quote())
    assert cost.total_msats == 840000
    assert cost.output_msats == cost.total_msats
    assert cost.total_usd == pytest.approx(0.042)
    assert cost.upstream_usd == 0.04
    assert cost.input_tokens == cost.output_tokens == 0


def test_explicit_zero_cost_is_free() -> None:
    assert calculate_image_cost(response(0), quote=quote()).total_msats == 0


def test_cost_exceeding_quote_is_capped_and_logged() -> None:
    q = quote()
    with patch("routstr.payment.images.logger.error") as log:
        cost = calculate_image_cost(response(0.1), quote=q)
    assert cost.total_msats == q.reserved_msats
    assert cost.upstream_usd == 0.1
    log.assert_called_once()
    assert "exceeded" in log.call_args.args[0]


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [], "usage": {"cost": 0.04}},
        {"data": [{"b64_json": ""}], "usage": {"cost": 0.04}},
        {"data": [{"b64_json": "invalid!"}], "usage": {"cost": 0.04}},
        {"data": [{"url": "https://example.com/output.png"}], "usage": {"cost": 0.04}},
        {"error": {}, **response()},
        {"type": "partial_image", "b64_json": "aQ=="},
        {"data": response()["data"]},
        response(-0.1),
        response(True),
        response(float("nan")),
        response(float("inf")),
        response(0.04, 2),
    ],
)
def test_incomplete_or_invalid_response_never_becomes_a_charge(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ImageRequestError):
        calculate_image_cost(payload, quote=quote())


@pytest.mark.asyncio
async def test_precomputed_cost_bypasses_token_parser_and_duplicate_claim() -> None:
    import routstr.auth as auth
    from routstr.auth import ReservationSnapshot
    from routstr.core.db import ApiKey

    key = ApiKey(hashed_key="test", balance=10000, reserved_balance=1000)
    reservation = ReservationSnapshot(
        release_id="image-r",
        key_hash="test",
        billing_key_hash="test",
        reserved_msats=1000,
    )
    cost = CostData(base_msats=0, input_msats=0, output_msats=500, total_msats=500)
    with (
        patch.object(auth, "_validate_reservation_snapshot", new=AsyncMock()),
        patch.object(
            auth, "_claim_reservation_for_charge", new=AsyncMock(return_value=False)
        ) as claim,
        patch.object(auth, "calculate_cost", new=AsyncMock()) as token_parser,
    ):
        result = await auth.adjust_payment_for_tokens(
            key,
            {},
            AsyncMock(),
            99999,
            reservation_snapshot=reservation,
            precomputed_cost=cost,
        )
    assert result["charged_msats"] == 0
    assert key.balance == 10000 and key.reserved_balance == 1000
    assert cost.charged_msats is None
    claim.assert_awaited_once()
    token_parser.assert_not_awaited()


@pytest.mark.asyncio
async def test_precomputed_cost_checks_authoritative_reservation_before_claim() -> None:
    import routstr.auth as auth
    from routstr.auth import ReservationSnapshot
    from routstr.core.db import ApiKey

    key = ApiKey(hashed_key="test", balance=10000)
    reservation = ReservationSnapshot(
        release_id="image-r",
        key_hash="test",
        billing_key_hash="test",
        reserved_msats=1000,
    )
    cost = CostData(base_msats=0, input_msats=0, output_msats=1001, total_msats=1001)
    with (
        patch.object(auth, "_validate_reservation_snapshot", new=AsyncMock()),
        patch.object(auth, "_claim_reservation_for_charge", new=AsyncMock()) as claim,
    ):
        with pytest.raises(ValueError, match="authoritative reservation"):
            await auth.adjust_payment_for_tokens(
                key,
                {},
                AsyncMock(),
                99999,
                reservation_snapshot=reservation,
                precomputed_cost=cost,
            )
    claim.assert_not_awaited()


def test_catalogue_quoteability_excludes_token_models() -> None:
    from routstr.payment.images import capability_is_quoteable

    assert capability_is_quoteable(capability())
    assert not capability_is_quoteable(capability(unit="token"))
    assert not capability_is_quoteable({"endpoints": []})


@pytest.mark.asyncio
async def test_precomputed_zero_cost_releases_and_duplicate_does_not_charge_twice() -> (
    None
):
    import routstr.auth as auth
    from routstr.auth import ReservationSnapshot
    from routstr.core.db import ApiKey

    key = ApiKey(hashed_key="test", balance=10000, reserved_balance=1000)
    reservation = ReservationSnapshot(
        release_id="image-r",
        key_hash="test",
        billing_key_hash="test",
        reserved_msats=1000,
    )
    cost = CostData(base_msats=0, input_msats=0, output_msats=0, total_msats=0)

    async def charge_rows(
        session: AsyncSession, *, charge_msats: int, **kwargs: Any
    ) -> bool:
        key.reserved_balance -= 1000
        key.balance -= charge_msats
        return True

    with (
        patch.object(auth, "_validate_reservation_snapshot", new=AsyncMock()),
        patch.object(
            auth,
            "_claim_reservation_for_charge",
            new=AsyncMock(side_effect=[True, False]),
        ),
        patch.object(
            auth, "_charge_reservation_rows", new=AsyncMock(side_effect=charge_rows)
        ) as charge,
        patch.object(auth, "_stop_reservation_heartbeat", new=AsyncMock()),
        patch.object(auth, "calculate_cost", new=AsyncMock()) as parser,
    ):
        first = await auth.adjust_payment_for_tokens(
            key,
            {},
            AsyncMock(),
            1000,
            reservation_snapshot=reservation,
            precomputed_cost=cost,
        )
        second = await auth.adjust_payment_for_tokens(
            key,
            {},
            AsyncMock(),
            1000,
            reservation_snapshot=reservation,
            precomputed_cost=cost,
        )
    assert first["charged_msats"] == second["charged_msats"] == 0
    assert key.balance == 10000 and key.reserved_balance == 0
    charge.assert_awaited_once()
    parser.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_msats", [0, 500])
async def test_precomputed_settlement_is_idempotent_in_database(
    actual_msats: int,
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel
    from sqlmodel.ext.asyncio.session import AsyncSession

    import routstr.auth as auth
    from routstr.core.db import ApiKey

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            key = ApiKey(hashed_key="image-idempotency", balance=10000)
            session.add(key)
            await session.commit()
            with (
                patch.object(auth, "_start_reservation_heartbeat"),
                patch.object(auth, "_stop_reservation_heartbeat", new=AsyncMock()),
                patch.object(auth, "accumulate_routstr_fee", new=AsyncMock()),
            ):
                reservation = await auth.pay_for_request(key, 1000, session)
                cost = CostData(
                    base_msats=0,
                    input_msats=0,
                    output_msats=actual_msats,
                    total_msats=actual_msats,
                )
                first = await auth.adjust_payment_for_tokens(
                    key,
                    {},
                    session,
                    1000,
                    reservation_snapshot=reservation,
                    precomputed_cost=cost,
                )
                second = await auth.adjust_payment_for_tokens(
                    key,
                    {},
                    session,
                    1000,
                    reservation_snapshot=reservation,
                    precomputed_cost=cost,
                )
                await session.refresh(key)
                assert first["charged_msats"] == actual_msats
                assert second["charged_msats"] == 0
                assert key.balance == 10000 - actual_msats
                assert key.reserved_balance == 0
                assert key.total_spent == actual_msats
                assert key.total_requests == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("second_unit", ["image", "token"])
def test_duplicate_provider_tags_cannot_select_cheapest_record(
    second_unit: str,
) -> None:
    import copy

    cap = capability()
    second = copy.deepcopy(cap["endpoints"][0])
    second["pricing"][0].update(unit=second_unit, cost_usd=0.1)
    cap["endpoints"].append(second)
    with pytest.raises(ImageRequestError) as error:
        quote(capabilities=cap)
    assert error.value.code == "image_pricing_unbounded"


def test_request_unit_not_assumed_to_mean_per_output_image() -> None:
    with pytest.raises(ImageRequestError):
        quote(capabilities=capability(unit="request"))
