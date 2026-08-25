"""Tests for the model prioritization algorithm."""

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Set required env vars before importing
os.environ["UPSTREAM_BASE_URL"] = "http://test"
os.environ["UPSTREAM_API_KEY"] = "test"

from routstr.algorithm import (  # noqa: E402
    calculate_model_cost_score,
    create_model_mappings,
    get_provider_penalty,
)
from routstr.core.db import get_session  # noqa: E402
from routstr.payment.models import (  # noqa: E402
    Architecture,
    Model,
    Pricing,
    models_router,
)


def create_test_model(
    model_id: str,
    prompt_price: float = 0.001,
    completion_price: float = 0.002,
    request_price: float = 0.0,
) -> Model:
    """Helper to create a test model with given pricing."""
    return Model(
        id=model_id,
        name=f"Test {model_id}",
        created=1234567890,
        description="Test model",
        context_length=8192,
        architecture=Architecture(
            modality="text",
            input_modalities=["text"],
            output_modalities=["text"],
            tokenizer="gpt",
            instruct_type=None,
        ),
        pricing=Pricing(
            prompt=prompt_price,
            completion=completion_price,
            request=request_price,
            image=0.0,
            web_search=0.0,
            internal_reasoning=0.0,
        ),
    )


def create_test_provider(
    name: str,
    base_url: str = "http://test.com",
    *,
    db_id: int | None = None,
    models: list[Model] | None = None,
    upstream_name: str | None = None,
    provider_fee: float = 1.0,
) -> Mock:
    """Helper to create a test provider mock."""
    provider = Mock()
    provider.provider_type = name
    provider.base_url = base_url
    provider.db_id = db_id
    provider.upstream_name = upstream_name or name
    provider.provider_fee = provider_fee
    provider.get_cached_models.return_value = models or []
    return provider


def test_calculate_model_cost_score_basic() -> None:
    """Test basic cost calculation."""
    model = create_test_model("test-model", prompt_price=0.001, completion_price=0.002)
    cost = calculate_model_cost_score(model)

    # Expected: (1000 tokens * 0.001) + (500 tokens * 0.002) = 0.001 + 0.001 = 0.002
    assert cost == 0.002


def test_calculate_model_cost_score_with_request_fee() -> None:
    """Test cost calculation with request fee."""
    model = create_test_model(
        "test-model",
        prompt_price=0.001,
        completion_price=0.002,
        request_price=0.0005,
    )
    cost = calculate_model_cost_score(model)

    # Expected: 0.001 + 0.001 + 0.0005 = 0.0025
    assert cost == 0.0025


def test_calculate_model_cost_score_expensive_model() -> None:
    """Test cost calculation for expensive model."""
    model = create_test_model(
        "expensive-model", prompt_price=0.03, completion_price=0.06
    )
    cost = calculate_model_cost_score(model)

    # Expected: (1000 * 0.03) + (500 * 0.06) = 0.03 + 0.03 = 0.06
    assert cost == 0.06


def test_get_provider_penalty_regular_provider() -> None:
    """Test penalty for regular provider."""
    provider = create_test_provider("regular-provider", "http://provider.com")
    penalty = get_provider_penalty(provider)
    assert penalty == 1.0


def test_get_provider_penalty_openrouter() -> None:
    """Test penalty for OpenRouter."""
    provider = create_test_provider("openrouter", "https://openrouter.ai/api/v1")
    penalty = get_provider_penalty(provider)
    assert penalty == 1.001


def test_create_model_mappings_advertises_cheapest_custom_provider_regardless_of_order() -> (
    None
):
    """The public model catalog must use the same cheapest custom-provider model."""
    cheap_model = create_test_model(
        "shared-model", prompt_price=0.001, completion_price=0.001
    )
    expensive_model = create_test_model(
        "shared-model", prompt_price=0.1, completion_price=0.1
    )
    cheap_provider = create_test_provider(
        "custom-cheap",
        "https://cheap.example/v1",
        db_id=1,
        models=[cheap_model],
    )
    expensive_provider = create_test_provider(
        "custom-expensive",
        "https://expensive.example/v1",
        db_id=2,
        models=[expensive_model],
    )

    provider_orders: list[list[Any]] = [
        [cheap_provider, expensive_provider],
        [expensive_provider, cheap_provider],
    ]
    for providers in provider_orders:
        _, provider_map, unique_models = create_model_mappings(
            upstreams=providers,
            overrides_by_key={},
            disabled_model_keys=set(),
        )

        assert unique_models["shared-model"].pricing.prompt == 0.001
        assert provider_map["shared-model"][0] == (cheap_model, cheap_provider)


def test_create_model_mappings_advertises_cheaper_openrouter_model() -> None:
    """OpenRouter may win when its adjusted price beats every custom provider."""
    custom_model = create_test_model(
        "shared-model", prompt_price=0.01, completion_price=0.01
    )
    openrouter_model = create_test_model(
        "shared-model", prompt_price=0.001, completion_price=0.001
    )
    custom = create_test_provider(
        "custom", "https://custom.example/v1", db_id=1, models=[custom_model]
    )
    openrouter = create_test_provider(
        "openrouter",
        "https://openrouter.ai/api/v1",
        db_id=2,
        models=[openrouter_model],
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[openrouter, custom],
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    assert unique_models["shared-model"].pricing.prompt == 0.001
    assert provider_map["shared-model"][0] == (openrouter_model, openrouter)


def test_create_model_mappings_ranks_multiple_openrouter_credentials() -> None:
    """OpenRouter accounts sharing a URL still compete by effective model cost."""
    cheap_model = create_test_model(
        "shared-model", prompt_price=0.001, completion_price=0.001
    )
    expensive_model = create_test_model(
        "shared-model", prompt_price=0.01, completion_price=0.01
    )
    cheap = create_test_provider(
        "openrouter-cheap",
        "https://openrouter.ai/api/v1",
        db_id=1,
        models=[cheap_model],
    )
    expensive = create_test_provider(
        "openrouter-expensive",
        "https://openrouter.ai/api/v1",
        db_id=2,
        models=[expensive_model],
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[cheap, expensive],
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    assert provider_map["shared-model"][0] == (cheap_model, cheap)
    assert unique_models["shared-model"].upstream_provider_id == "openrouter-cheap"


def test_create_model_mappings_uses_openrouter_penalty_for_catalog_ties() -> None:
    """Equal raw prices prefer a custom provider in routing and the catalog."""
    custom_model = create_test_model("shared-model")
    openrouter_model = create_test_model("shared-model")
    custom = create_test_provider(
        "custom", "https://custom.example/v1", db_id=1, models=[custom_model]
    )
    openrouter = create_test_provider(
        "openrouter",
        "https://openrouter.ai/api/v1",
        db_id=2,
        models=[openrouter_model],
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[openrouter, custom],
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    assert unique_models["shared-model"].upstream_provider_id == "custom"
    assert provider_map["shared-model"][0] == (custom_model, custom)


def test_create_model_mappings_applies_custom_provider_fees_before_advertising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom-provider DB fees participate in cheapest-model selection."""
    providers: list[Any] = [
        create_test_provider(
            "high-fee",
            "https://high-fee.example/v1",
            db_id=1,
            models=[create_test_model("shared-model")],
            provider_fee=1.20,
        ),
        create_test_provider(
            "low-fee",
            "https://low-fee.example/v1",
            db_id=2,
            models=[create_test_model("shared-model")],
            provider_fee=1.01,
        ),
    ]
    rows = {
        1: SimpleNamespace(id="shared-model", upstream_provider_id=1, enabled=True),
        2: SimpleNamespace(id="shared-model", upstream_provider_id=2, enabled=True),
    }

    def fake_row_to_model(row, *, apply_provider_fee, provider_fee) -> Model:  # type: ignore[no-untyped-def]
        assert apply_provider_fee is True
        return create_test_model(
            row.id,
            prompt_price=0.001 * provider_fee,
            completion_price=0.002 * provider_fee,
        )

    monkeypatch.setattr("routstr.payment.models._row_to_model", fake_row_to_model)

    _, provider_map, unique_models = create_model_mappings(
        upstreams=providers,
        overrides_by_key={
            ("shared-model", provider_id): (
                row,
                providers[provider_id - 1].provider_fee,
            )
            for provider_id, row in rows.items()
        },
        disabled_model_keys=set(),
    )

    assert unique_models["shared-model"].upstream_provider_id == "low-fee"
    assert provider_map["shared-model"][0][1] is providers[1]


def test_create_model_mappings_compares_missing_custom_override_with_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom model created in the DB can beat a close OpenRouter candidate."""
    custom = create_test_provider(
        "custom", "https://custom.example/v1", db_id=1, models=[]
    )
    openrouter_model = create_test_model(
        "shared-model", prompt_price=0.0009995, completion_price=0.0009995
    )
    openrouter = create_test_provider(
        "openrouter",
        "https://openrouter.ai/api/v1",
        db_id=2,
        models=[openrouter_model],
    )
    custom_override = create_test_model(
        "shared-model", prompt_price=0.001, completion_price=0.001
    )
    override_row = SimpleNamespace(
        id="shared-model", upstream_provider_id=1, enabled=True
    )
    monkeypatch.setattr(
        "routstr.payment.models._row_to_model", lambda *args, **kwargs: custom_override
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[openrouter, custom],
        overrides_by_key={("shared-model", 1): (override_row, 1.0)},
        disabled_model_keys=set(),
    )

    assert unique_models["shared-model"].upstream_provider_id == "custom"
    assert provider_map["shared-model"][0] == (custom_override, custom)


def test_create_model_mappings_excludes_disabled_cheapest_custom_model() -> None:
    """A disabled provider-scoped model cannot become the advertised cheapest."""
    disabled_cheap = create_test_model(
        "shared-model", prompt_price=0.0001, completion_price=0.0001
    )
    enabled_expensive = create_test_model(
        "shared-model", prompt_price=0.01, completion_price=0.01
    )
    cheap_provider = create_test_provider(
        "disabled-cheap",
        "https://cheap.example/v1",
        db_id=1,
        models=[disabled_cheap],
    )
    enabled_provider = create_test_provider(
        "enabled",
        "https://enabled.example/v1",
        db_id=2,
        models=[enabled_expensive],
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[cheap_provider, enabled_provider],
        overrides_by_key={},
        disabled_model_keys={("shared-model", 1)},
    )

    assert unique_models["shared-model"].upstream_provider_id == "enabled"
    assert provider_map["shared-model"] == [(enabled_expensive, enabled_provider)]


def test_create_model_mappings_same_url_ranks_shared_model_and_keeps_unique_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-URL providers compete by cost without losing provider-only models."""
    shared_url = "https://custom.example/v1"
    high_fee = create_test_provider(
        "high-fee",
        shared_url,
        db_id=1,
        models=[create_test_model("shared-model", prompt_price=0.0012)],
        provider_fee=1.20,
    )
    low_fee = create_test_provider(
        "low-fee",
        shared_url,
        db_id=2,
        models=[create_test_model("shared-model", prompt_price=0.00101)],
        provider_fee=1.01,
    )
    unique_override = SimpleNamespace(
        id="unique-deployment", upstream_provider_id=1, enabled=True
    )
    override_model = create_test_model("unique-deployment", prompt_price=0.002)
    monkeypatch.setattr(
        "routstr.payment.models._row_to_model", Mock(return_value=override_model)
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[high_fee, low_fee],
        overrides_by_key={("unique-deployment", 1): (unique_override, 1.20)},
        disabled_model_keys=set(),
    )

    assert provider_map["shared-model"][0][1] is low_fee
    assert provider_map["unique-deployment"] == [(override_model, high_fee)]
    assert set(unique_models) == {"shared-model", "unique-deployment"}


def test_create_model_mappings_chooses_cheapest_shared_forwarded_id() -> None:
    """Custom deployment names sharing a public ID advertise the cheapest deployment."""
    expensive = create_test_model(
        "deployment-expensive", prompt_price=0.01, completion_price=0.01
    )
    expensive.forwarded_model_id = "public-model"
    cheap = create_test_model(
        "deployment-cheap", prompt_price=0.001, completion_price=0.001
    )
    cheap.forwarded_model_id = "public-model"
    expensive_provider = create_test_provider(
        "expensive",
        "https://expensive.example/v1",
        db_id=1,
        models=[expensive],
    )
    cheap_provider = create_test_provider(
        "cheap", "https://cheap.example/v1", db_id=2, models=[cheap]
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[cheap_provider, expensive_provider],
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    assert unique_models["public-model"].pricing.prompt == 0.001
    assert provider_map["public-model"][0] == (cheap, cheap_provider)


def test_create_model_mappings_exact_model_id_beats_forwarded_id_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact model ID uses its cheapest provider before forwarded aliases."""
    direct_expensive = create_test_model(
        "public-model", prompt_price=0.01, completion_price=0.01
    )
    direct_cheapest = create_test_model(
        "public-model", prompt_price=0.001, completion_price=0.001
    )
    forwarded = create_test_model(
        "deployment-name", prompt_price=0.0001, completion_price=0.0001
    )
    forwarded.forwarded_model_id = "public-model"
    direct_expensive_provider = create_test_provider(
        "direct-expensive",
        "https://direct-expensive.example/v1",
        db_id=1,
        models=[direct_expensive],
    )
    direct_cheapest_provider = create_test_provider(
        "direct-cheapest",
        "https://direct-cheapest.example/v1",
        db_id=2,
        models=[direct_cheapest],
    )
    forwarded_provider = create_test_provider(
        "forwarded",
        "https://forwarded.example/v1",
        db_id=3,
        models=[forwarded],
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[
            direct_expensive_provider,
            forwarded_provider,
            direct_cheapest_provider,
        ],
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    assert provider_map["public-model"][0] == (
        direct_cheapest,
        direct_cheapest_provider,
    )
    assert unique_models["public-model"].pricing.prompt == 0.001
    assert unique_models["public-model"].upstream_provider_id == "direct-cheapest"

    import routstr.proxy as proxy

    monkeypatch.setattr(proxy, "_unique_models", unique_models)
    app = FastAPI()
    app.include_router(models_router)
    app.dependency_overrides[get_session] = lambda: None
    response = TestClient(app).get("/v1/models")

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "public-model"
    assert response.json()["data"][0]["pricing"]["prompt"] == 0.001


def test_models_endpoint_preserves_catalog_id_when_winner_forwards_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each catalog row keeps its requested ID while using its routing winner.

    ``foo`` is served directly by two providers: the cheaper one prefixes the ID
    (``vendor/foo``) and the pricier one exposes the bare ID while forwarding
    upstream to ``bar``. Prefix vs bare spelling must not decide the winner, so
    the cheaper prefixed provider wins ``foo`` while ``bar`` still appears as its
    own catalog row served by the forwarding provider.
    """
    base_alias = create_test_model(
        "vendor/foo", prompt_price=0.001, completion_price=0.001
    )
    redirected_exact = create_test_model("foo", prompt_price=0.1, completion_price=0.1)
    redirected_exact.forwarded_model_id = "bar"
    base_provider = create_test_provider(
        "base", "https://base.example/v1", db_id=1, models=[base_alias]
    )
    redirect_provider = create_test_provider(
        "redirect", "https://redirect.example/v1", db_id=2, models=[redirected_exact]
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[base_provider, redirect_provider],
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    assert provider_map["foo"][0] == (base_alias, base_provider)
    assert unique_models["foo"].id == "foo"
    assert unique_models["foo"].upstream_provider_id == "base"

    import routstr.proxy as proxy

    monkeypatch.setattr(proxy, "_unique_models", unique_models)
    app = FastAPI()
    app.include_router(models_router)
    app.dependency_overrides[get_session] = lambda: None
    response = TestClient(app).get("/v1/models")

    assert response.status_code == 200
    assert {model["id"] for model in response.json()["data"]} == {"foo", "bar"}

    with patch(
        "routstr.upstream.model_paths.get_paths_for_model",
        new=AsyncMock(return_value={"data": []}),
    ):
        path_response = TestClient(app).get(
            "/v1/models/paths/model", params={"model_id": "foo"}
        )
    assert path_response.status_code == 200


def test_models_endpoint_dedupes_case_insensitive_model_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case-insensitive routing aliases produce one catalog row, not duplicates."""
    upper = create_test_model("GPT-4o", prompt_price=0.001, completion_price=0.001)
    lower = create_test_model("gpt-4o", prompt_price=0.01, completion_price=0.01)
    upper_provider = create_test_provider(
        "upper", "https://upper.example/v1", db_id=1, models=[upper]
    )
    lower_provider = create_test_provider(
        "lower", "https://lower.example/v1", db_id=2, models=[lower]
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[upper_provider, lower_provider],
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    assert list(unique_models) == ["gpt-4o"]
    assert provider_map["gpt-4o"][0] == (upper, upper_provider)

    import routstr.proxy as proxy

    monkeypatch.setattr(proxy, "_unique_models", unique_models)
    app = FastAPI()
    app.include_router(models_router)
    app.dependency_overrides[get_session] = lambda: None
    response = TestClient(app).get("/v1/models")

    assert response.status_code == 200
    assert [model["id"] for model in response.json()["data"]] == ["GPT-4o"]

    with patch(
        "routstr.upstream.model_paths.get_paths_for_model",
        new=AsyncMock(return_value={"data": []}),
    ):
        path_response = TestClient(app).get(
            "/v1/models/paths/model", params={"model_id": "gpt-4o"}
        )
    assert path_response.status_code == 200


def test_create_model_mappings_equal_custom_prices_remain_cheapest() -> None:
    """Tied custom providers advertise one of the equally cheapest candidates."""
    first_model = create_test_model("shared-model")
    second_model = create_test_model("shared-model")
    first = create_test_provider(
        "first", "https://first.example/v1", db_id=1, models=[first_model]
    )
    second = create_test_provider(
        "second", "https://second.example/v1", db_id=2, models=[second_model]
    )

    provider_orders: list[list[Any]] = [[first, second], [second, first]]
    for providers in provider_orders:
        _, provider_map, unique_models = create_model_mappings(
            upstreams=providers,
            overrides_by_key={},
            disabled_model_keys=set(),
        )

        assert calculate_model_cost_score(unique_models["shared-model"]) == min(
            calculate_model_cost_score(first_model),
            calculate_model_cost_score(second_model),
        )
        assert (
            unique_models["shared-model"].upstream_provider_id
            == provider_map["shared-model"][0][1].provider_type
        )


def test_models_endpoint_returns_cheapest_custom_provider_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /v1/models exposes the cheapest model chosen from custom providers."""
    cheap = create_test_model(
        "shared-model", prompt_price=0.001, completion_price=0.001
    )
    expensive = create_test_model(
        "shared-model", prompt_price=0.1, completion_price=0.1
    )
    providers: list[Any] = [
        create_test_provider(
            "cheap", "https://cheap.example/v1", db_id=1, models=[cheap]
        ),
        create_test_provider(
            "expensive",
            "https://expensive.example/v1",
            db_id=2,
            models=[expensive],
        ),
    ]
    _, _, unique_models = create_model_mappings(
        upstreams=providers,
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    import routstr.proxy as proxy

    monkeypatch.setattr(proxy, "_unique_models", unique_models)
    app = FastAPI()
    app.include_router(models_router)
    app.dependency_overrides[get_session] = lambda: None

    response = TestClient(app).get("/v1/models")

    assert response.status_code == 200
    assert len(response.json()["data"]) == 1
    assert response.json()["data"][0]["pricing"]["prompt"] == 0.001
    assert response.json()["data"][0]["upstream_provider_id"] == "cheap"


def test_create_model_mappings_includes_db_override_for_missing_cached_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model overrides should still map when provider discovery misses the model."""
    provider = create_test_provider(
        "azure",
        "https://example.openai.azure.com/openai/v1",
        db_id=7,
        models=[],
    )
    override_model = create_test_model("azure/gpt-4o")
    override_model.canonical_slug = "azure-deployment"

    def fake_row_to_model(*args, **kwargs) -> Model:  # type: ignore[no-untyped-def]
        return override_model

    monkeypatch.setattr("routstr.payment.models._row_to_model", fake_row_to_model)

    override_row = SimpleNamespace(
        id="azure/gpt-4o", upstream_provider_id=7, enabled=True
    )

    model_instances, provider_map, unique_models = create_model_mappings(
        upstreams=[provider],
        overrides_by_key={("azure/gpt-4o", 7): (override_row, 1.01)},
        disabled_model_keys=set(),
    )

    assert "azure/gpt-4o" in model_instances
    assert [p for _, p in provider_map["azure/gpt-4o"]] == [provider]
    assert "gpt-4o" in unique_models


def test_create_model_mappings_dedupes_with_provider_identity_not_provider_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different provider instances of same type should both survive dedupe."""
    provider_a_model = create_test_model(
        "azure/gpt-4o", prompt_price=0.01, completion_price=0.01
    )
    provider_a = create_test_provider(
        "azure",
        "https://a.openai.azure.com/openai/v1",
        db_id=1,
        models=[provider_a_model],
        upstream_name="azure-a",
    )
    provider_b = create_test_provider(
        "azure",
        "https://b.openai.azure.com/openai/v1",
        db_id=2,
        models=[],
        upstream_name="azure-b",
    )

    override_model = create_test_model(
        "azure/gpt-4o", prompt_price=0.001, completion_price=0.001
    )
    override_model.canonical_slug = "azure-b-deployment"

    def fake_row_to_model(*args, **kwargs) -> Model:  # type: ignore[no-untyped-def]
        return override_model

    monkeypatch.setattr("routstr.payment.models._row_to_model", fake_row_to_model)

    override_row = SimpleNamespace(
        id="azure/gpt-4o", upstream_provider_id=2, enabled=True
    )

    _, provider_map, _ = create_model_mappings(
        upstreams=[provider_a, provider_b],
        overrides_by_key={("azure/gpt-4o", 2): (override_row, 1.01)},
        disabled_model_keys=set(),
    )

    providers_for_alias = [p for _, p in provider_map["azure/gpt-4o"]]
    assert provider_a in providers_for_alias
    assert provider_b in providers_for_alias
    assert len(providers_for_alias) == 2


def test_create_model_mappings_applies_override_only_to_matching_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-id overrides must not add provider-specific aliases to other providers."""
    provider_a_model = create_test_model("same-id", prompt_price=0.01)
    provider_a = create_test_provider(
        "provider-a",
        "https://provider-a.example/v1",
        db_id=1,
        models=[provider_a_model],
    )
    provider_b_model = create_test_model("same-id", prompt_price=0.02)
    provider_b = create_test_provider(
        "provider-b",
        "https://provider-b.example/v1",
        db_id=2,
        models=[provider_b_model],
    )

    override_model = create_test_model("same-id", prompt_price=0.001)
    override_model.alias_ids = ["provider-b-only"]
    override_row = SimpleNamespace(id="same-id", upstream_provider_id=2, enabled=True)

    def fake_row_to_model(*args, **kwargs) -> Model:  # type: ignore[no-untyped-def]
        return override_model

    monkeypatch.setattr("routstr.payment.models._row_to_model", fake_row_to_model)

    _, provider_map, _ = create_model_mappings(
        upstreams=[provider_a, provider_b],
        overrides_by_key={("same-id", 2): (override_row, 1.01)},
        disabled_model_keys=set(),
    )

    assert [p for _, p in provider_map["provider-b-only"]] == [provider_b]
    assert {p for _, p in provider_map["same-id"]} == {provider_a, provider_b}


def test_create_model_mappings_does_not_split_self_alias_from_base_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write-time self-alias must not advertise one shared model twice (#639)."""
    model_id = "deepseek/deepseek-chat"
    provider_a = create_test_provider(
        "provider-a",
        "https://provider-a.example/v1",
        db_id=1,
        models=[create_test_model(model_id)],
    )
    provider_b = create_test_provider(
        "provider-b",
        "https://provider-b.example/v1",
        db_id=2,
        models=[
            create_test_model(model_id, prompt_price=0.0001, completion_price=0.0001)
        ],
    )

    admin_saved_model = create_test_model(
        model_id, prompt_price=1.0, completion_price=1.0
    )
    admin_saved_model.forwarded_model_id = model_id
    override_row = SimpleNamespace(id=model_id, upstream_provider_id=1, enabled=True)

    def fake_row_to_model(*args, **kwargs) -> Model:  # type: ignore[no-untyped-def]
        return admin_saved_model

    monkeypatch.setattr("routstr.payment.models._row_to_model", fake_row_to_model)

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[provider_a, provider_b],
        overrides_by_key={(model_id, 1): (override_row, 1.0)},
        disabled_model_keys=set(),
    )

    advertised_ids = sorted(
        model.forwarded_model_id or model.id for model in unique_models.values()
    )
    assert advertised_ids == ["deepseek-chat"]
    assert provider_map[model_id][0][1] is provider_b


def test_create_model_mappings_preserves_case_only_forwarded_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A case-only alias is distinct and must not be normalized as a self-alias."""
    model_id = "deepseek/deepseek-chat"
    case_only_alias = "DeepSeek/DeepSeek-Chat"
    provider = create_test_provider(
        "provider-a",
        "https://provider-a.example/v1",
        db_id=1,
        models=[create_test_model(model_id)],
    )

    override_model = create_test_model(model_id)
    override_model.forwarded_model_id = case_only_alias
    override_row = SimpleNamespace(id=model_id, upstream_provider_id=1, enabled=True)

    def fake_row_to_model(*args, **kwargs) -> Model:  # type: ignore[no-untyped-def]
        return override_model

    monkeypatch.setattr("routstr.payment.models._row_to_model", fake_row_to_model)

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[provider],
        overrides_by_key={(model_id, 1): (override_row, 1.0)},
        disabled_model_keys=set(),
    )

    normalized_alias = case_only_alias.lower()
    assert list(unique_models) == [normalized_alias]
    assert unique_models[normalized_alias].forwarded_model_id == case_only_alias
    assert len(provider_map[case_only_alias.lower()]) == 1


def test_create_model_mappings_disables_only_matching_provider() -> None:
    """Disabled overrides are scoped to the provider row, not the shared model id."""
    provider_a = create_test_provider(
        "provider-a",
        "https://provider-a.example/v1",
        db_id=1,
        models=[create_test_model("same-id")],
    )
    provider_b = create_test_provider(
        "provider-b",
        "https://provider-b.example/v1",
        db_id=2,
        models=[create_test_model("same-id")],
    )

    _, provider_map, _ = create_model_mappings(
        upstreams=[provider_a, provider_b],
        overrides_by_key={},
        disabled_model_keys={("same-id", 2)},
    )

    assert [p for _, p in provider_map["same-id"]] == [provider_a]


def test_create_model_mappings_prefixed_openrouter_beats_bare_tinfoil_id() -> None:
    """Prefix-vs-bare ID spelling must not outrank price for the same model.

    Tinfoil advertises bare model IDs (``gpt-oss-120b``) while OpenRouter keeps
    the org prefix (``openai/gpt-oss-120b``). Both serve the same model, so the
    cheaper OpenRouter deployment must win the public ``gpt-oss-120b`` catalog
    row and route; the bare-ID exact match must not shadow it on spelling alone.
    """
    tinfoil_expensive = create_test_model(
        "gpt-oss-120b", prompt_price=0.01, completion_price=0.01
    )
    openrouter_cheap = create_test_model(
        "openai/gpt-oss-120b", prompt_price=0.001, completion_price=0.001
    )
    tinfoil = create_test_provider(
        "tinfoil",
        "https://inference.tinfoil.sh/v1",
        db_id=1,
        models=[tinfoil_expensive],
    )
    openrouter = create_test_provider(
        "openrouter",
        "https://openrouter.ai/api/v1",
        db_id=2,
        models=[openrouter_cheap],
    )

    # Discovery order should not matter: Tinfoil (non-OpenRouter) is processed
    # first, yet the cheaper OpenRouter candidate must still win.
    _, provider_map, unique_models = create_model_mappings(
        upstreams=[tinfoil, openrouter],
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    assert provider_map["gpt-oss-120b"][0] == (openrouter_cheap, openrouter)
    assert unique_models["gpt-oss-120b"].upstream_provider_id == "openrouter"
    assert unique_models["gpt-oss-120b"].pricing.prompt == 0.001


def test_create_model_mappings_uppercase_prefixed_base_keeps_top_tier() -> None:
    """Uppercase prefixed IDs still match the public alias at the direct tier.

    ``Qwen/Qwen2.5-72B`` lowercases to alias ``qwen2.5-72b``; its base name
    must be compared case-insensitively so it stays a direct match instead of
    falling to the weakest tier and losing to a forwarded alias on spelling.
    """
    prefixed_cheap = create_test_model(
        "Qwen/Qwen2.5-72B", prompt_price=0.001, completion_price=0.001
    )
    forwarded_expensive = create_test_model(
        "deployment-x", prompt_price=0.1, completion_price=0.1
    )
    forwarded_expensive.forwarded_model_id = "qwen2.5-72b"
    prefixed_provider = create_test_provider(
        "prefixed", "https://prefixed.example/v1", db_id=1, models=[prefixed_cheap]
    )
    forwarded_provider = create_test_provider(
        "forwarded", "https://forwarded.example/v1", db_id=2, models=[forwarded_expensive]
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[forwarded_provider, prefixed_provider],
        overrides_by_key={},
        disabled_model_keys=set(),
    )

    assert provider_map["qwen2.5-72b"][0] == (prefixed_cheap, prefixed_provider)
    assert unique_models["qwen2.5-72b"].upstream_provider_id == "prefixed"


def test_create_model_mappings_excludes_a_malformed_price() -> None:
    """A rate that is not a number must not be routable.

    A negative or non-finite rate reads as a real price to every truthiness
    check, so the candidate was built into the map and served. The cost
    calculation cannot price on such a rate, so every request on the model fell
    through to the flat maximum reservation — or, for a negative rate, billed a
    negative amount that settlement credits back to the caller.
    """
    healthy = create_test_model("healthy-model")
    for bad_rate in (float("nan"), float("inf"), -1.0):
        broken = create_test_model("broken-model", prompt_price=bad_rate)
        provider = create_test_provider(
            "custom",
            "https://custom.example/v1",
            db_id=1,
            models=[broken, healthy],
        )

        _, provider_map, unique_models = create_model_mappings(
            upstreams=[provider],
            overrides_by_key={},
            disabled_model_keys=set(),
        )

        assert "broken-model" not in provider_map, bad_rate
        assert "broken-model" not in unique_models, bad_rate
        # One unroutable candidate must not cost the provider its other models.
        assert "healthy-model" in provider_map, bad_rate


def test_create_model_mappings_excludes_an_override_with_a_malformed_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override row carrying a malformed rate is unroutable too.

    An override replaces the discovered model's price, so a provider whose
    catalog is sound still routes at whatever the row says. The guard has to sit
    after the override is applied, not before it.
    """
    discovered = create_test_model("shared-model")
    provider = create_test_provider(
        "custom", "https://custom.example/v1", db_id=3, models=[discovered]
    )
    override_model = create_test_model("shared-model", prompt_price=float("-inf"))

    monkeypatch.setattr(
        "routstr.payment.models._row_to_model",
        lambda *args, **kwargs: override_model,
    )
    override_row = SimpleNamespace(
        id="shared-model", upstream_provider_id=3, enabled=True
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[provider],
        overrides_by_key={("shared-model", 3): (override_row, 1.0)},
        disabled_model_keys=set(),
    )

    assert "shared-model" not in provider_map
    assert "shared-model" not in unique_models


def test_create_model_mappings_survives_an_unreadable_override_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One row that cannot be read must not empty the whole routing map.

    Stored pricing is JSON from whatever wrote the row, so converting it can
    raise. Converting an override while walking a provider's catalog let that
    exception unwind the entire map build: at boot the node came up routing
    nothing, and on a later refresh the map it already had went permanently
    stale. The sibling loop over override-only rows already skips and logs such
    a row.
    """
    broken = create_test_model("broken-model")
    healthy = create_test_model("healthy-model")
    provider = create_test_provider(
        "custom",
        "https://custom.example/v1",
        db_id=5,
        models=[broken, healthy],
    )

    def raising_row_to_model(row: Any, *args: Any, **kwargs: Any) -> Model:
        raise ValueError("value is not a valid float")

    monkeypatch.setattr("routstr.payment.models._row_to_model", raising_row_to_model)
    override_row = SimpleNamespace(
        id="broken-model", upstream_provider_id=5, enabled=True
    )

    _, provider_map, unique_models = create_model_mappings(
        upstreams=[provider],
        overrides_by_key={("broken-model", 5): (override_row, 1.0)},
        disabled_model_keys=set(),
    )

    assert "broken-model" not in provider_map
    assert "healthy-model" in provider_map
    assert "healthy-model" in unique_models
