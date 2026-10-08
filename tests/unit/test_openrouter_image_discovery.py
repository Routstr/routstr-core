"""Capability discovery is additive, outage-tolerant and fail-closed on prices."""

import asyncio
from typing import Any

import httpx
import pytest
from pydantic.v1 import ValidationError

from routstr.modalities import ModalityPricingLine
from routstr.payment.models import (
    Model,
    _has_valid_pricing,
    async_fetch_openrouter_models,
)
from routstr.upstream.openrouter_catalog import fetch_openrouter_catalog


def record(
    model_id: str = "vendor/image", outputs: list[str] | None = None
) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id,
        "description": "",
        "created": 0,
        "context_length": 0,
        "architecture": {
            "modality": "text->image",
            "input_modalities": ["text"],
            "output_modalities": outputs or ["image"],
            "tokenizer": "unknown",
            "instruct_type": None,
        },
        "pricing": {"prompt": "0", "completion": "0"},
    }


def endpoint(rate: Any = "0.04", unit: str = "image") -> dict[str, Any]:
    return {
        "provider_slug": "vendor",
        "provider_tag": "vendor",
        "supported_parameters": {"n": {"type": "range", "min": 1, "max": 4}},
        "pricing": [{"billable": "output_image", "unit": unit, "cost_usd": rate}],
    }


def transport(
    main: list[dict[str, Any]],
    images: list[Any] | None = None,
    endpoints: list[Any] | None = None,
    image_status: int = 200,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/models":
            assert request.url.params["output_modalities"] == "text,image"
            return httpx.Response(200, json={"data": main})
        if path == "/api/v1/embeddings/models":
            return httpx.Response(200, json={"data": []})
        if path == "/api/v1/images/models":
            return httpx.Response(image_status, json={"data": images or []})
        return httpx.Response(
            200, json={"id": "vendor/image", "endpoints": endpoints or []}
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_merges_unit_pricing_without_fabricating_general_records() -> None:
    image = {
        "id": "vendor/image",
        "supported_parameters": {"n": {"type": "range", "min": 1, "max": 4}},
        "supports_streaming": True,
    }
    async with httpx.AsyncClient(
        transport=transport(
            [record()], [image, {"id": "missing/general"}], [endpoint(unit="token")]
        )
    ) as client:
        result = await fetch_openrouter_catalog(client)
    assert len(result) == 1
    model = Model.parse_obj(result[0])
    capability = model.api_capabilities["images"]
    assert capability.supports_streaming
    assert capability.endpoints[0].pricing[0].unit == "token"
    assert capability.endpoints[0].pricing[0].cost_usd == 0.04
    assert _has_valid_pricing(result[0])
    assert (
        model.dict()["api_capabilities"]["images"]["endpoints"][0]["pricing"][0]["unit"]
        == "token"
    )


@pytest.mark.asyncio
async def test_image_outage_preserves_identical_text_record() -> None:
    text = record("vendor/chat", ["text"])
    text["pricing"]["prompt"] = "0.001"
    async with httpx.AsyncClient(
        transport=transport([text], image_status=503)
    ) as client:
        result = await fetch_openrouter_catalog(client)
    assert result == [text]
    assert "api_capabilities" not in Model.parse_obj(text).dict()


@pytest.mark.parametrize("bad", [True, -1, "NaN", "Infinity", {}, 10**400])
def test_rates_are_not_silently_free(bad: Any) -> None:
    with pytest.raises(ValidationError):
        ModalityPricingLine(billable="output_image", unit="image", cost_usd=bad)


@pytest.mark.asyncio
async def test_malformed_endpoint_is_removed_not_zero_priced() -> None:
    async with httpx.AsyncClient(
        transport=transport(
            [record()],
            [{"id": "vendor/image"}],
            [endpoint("NaN"), {**endpoint("0.02"), "provider_tag": "other"}],
        )
    ) as client:
        result = await fetch_openrouter_catalog(client)
    lines = result[0]["api_capabilities"]["images"]["endpoints"]
    assert len(lines) == 1
    assert lines[0]["pricing"][0]["cost_usd"] == 0.02


@pytest.mark.asyncio
async def test_discovered_image_models_remain_available_for_endpoint_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = [record(), record("vendor/dual", ["text", "image"])]
    main[1]["pricing"]["prompt"] = "0.001"
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda: original(
            transport=transport(main, [{"id": "vendor/image"}], [endpoint()])
        ),
    )
    result = await async_fetch_openrouter_models()
    assert result[0].get("enabled", True)
    assert result[1].get("enabled", True)


@pytest.mark.asyncio
async def test_endpoint_concurrency_is_bounded() -> None:
    active = peak = 0
    models = [record(f"vendor/image-{i}") for i in range(20)]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        path = request.url.path
        if path.endswith("/endpoints"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return httpx.Response(200, json={"endpoints": [endpoint()]})
        return httpx.Response(
            200, json={"data": [] if "embeddings" in path else models}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_openrouter_catalog(client)
    assert len(result) == 20
    assert peak <= 6


@pytest.mark.asyncio
async def test_slow_endpoint_enrichment_does_not_stall_refresh() -> None:
    text = record("vendor/text", ["text"])
    image = record()

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/endpoints"):
            await asyncio.sleep(10)
        if path == "/api/v1/models":
            return httpx.Response(200, json={"data": [text, image]})
        if path == "/api/v1/images/models":
            return httpx.Response(200, json={"data": [{"id": "vendor/image"}]})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await asyncio.wait_for(
            fetch_openrouter_catalog(client, timeout=0.01), 0.5
        )
    assert result == [text, image]


def test_script_delegates_to_shared_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import models_meta

    async def fake_catalogue(client: httpx.AsyncClient) -> list[dict[str, Any]]:
        return [record(), record("other/model")]

    monkeypatch.setattr(models_meta, "fetch_openrouter_catalog", fake_catalogue)
    result = models_meta.fetch_openrouter_models("vendor")
    assert [item["id"] for item in result] == ["image"]
    assert result[0].get("enabled", True)


@pytest.mark.asyncio
async def test_malformed_duplicate_tag_cannot_be_quoted() -> None:
    from routstr.payment.images import ImageRequestError, quote_image_request

    async with httpx.AsyncClient(
        transport=transport(
            [record()],
            [{"id": "vendor/image"}],
            [endpoint("NaN"), endpoint("0.02")],
        )
    ) as client:
        result = await fetch_openrouter_catalog(client)
    capability = result[0]["api_capabilities"]["images"]
    with pytest.raises(ImageRequestError):
        quote_image_request(
            {"model": "vendor/image", "prompt": "a panda"},
            upstream_model_id="vendor/image",
            capabilities=capability,
            provider_fee=1.05,
            usd_per_sat=0.00005,
            max_request_usd=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [None, {}, {"provider_tag": 123}])
async def test_unidentifiable_endpoint_invalidates_capability(malformed: Any) -> None:
    async with httpx.AsyncClient(
        transport=transport(
            [record()], [{"id": "vendor/image"}], [endpoint(), malformed]
        )
    ) as client:
        result = await fetch_openrouter_catalog(client)
    assert "api_capabilities" not in result[0]
