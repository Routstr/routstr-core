"""Tests for pricing unit normalization across model sources."""

import json
from types import TracebackType

import pytest

from routstr.core.db import ModelRow
from routstr.payment.models import _row_to_model
from routstr.upstream.generic import GenericUpstreamProvider


def test_row_to_model_normalizes_legacy_per_million_db_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("routstr.payment.models.sats_usd_price", lambda: 0.00002)

    row = ModelRow(
        id="anthropic/claude-opus-4.5",
        upstream_provider_id=1,
        name="Claude Opus 4.5",
        created=0,
        description="test",
        context_length=200000,
        architecture=json.dumps(
            {
                "modality": "text->text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "test",
                "instruct_type": None,
            }
        ),
        pricing=json.dumps(
            {
                "prompt": 5.0,
                "completion": 25.0,
                "request": 0.0,
                "image": 0.0,
                "web_search": 0.0,
                "internal_reasoning": 0.0,
            }
        ),
        enabled=True,
    )

    model = _row_to_model(row)

    assert model.pricing.prompt == pytest.approx(0.000005)
    assert model.pricing.completion == pytest.approx(0.000025)


@pytest.mark.asyncio
async def test_generic_provider_missing_pricing_defaults_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "data": [
                    {
                        "id": "gpt-5-chat",
                        "name": "gpt-5-chat",
                        "created": 0,
                        "owned_by": "openai",
                        "model_spec": {},
                    }
                ]
            }

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            del exc_type, exc, tb
            return None

        async def get(
            self, url: str, headers: dict[str, str] | None = None
        ) -> FakeResponse:
            del url, headers
            return FakeResponse()

    monkeypatch.setattr(
        "routstr.upstream.generic.httpx.AsyncClient",
        lambda timeout=30.0: FakeClient(),
    )

    provider = GenericUpstreamProvider(base_url="https://example.test")

    models = await provider.fetch_models()

    assert len(models) == 1
    assert models[0].pricing.prompt == 0.0
    assert models[0].pricing.completion == 0.0
