"""Discovery is not a promise that every modality can be safely served."""

import time
from types import SimpleNamespace

from routstr.algorithm import create_model_mappings
from routstr.core.settings import settings
from routstr.payment.models import Architecture, Model, Pricing


def model(unit="image", *, dual=False, fetched_at=None):
    return Model(
        id="vendor/image-model", name="Image", created=0, description="test",
        context_length=0,
        architecture=Architecture(modality="text->image", input_modalities=["text"],
                                  output_modalities=["image", "text"] if dual else ["image"],
                                  tokenizer="unknown", instruct_type=None),
        pricing=Pricing(prompt=0, completion=0),
        api_capabilities={"images": {
            "fetched_at": int(time.time()) if fetched_at is None else fetched_at,
            "endpoints": [{"provider_slug": "vendor", "provider_tag": "vendor",
                           "supported_parameters": {},
                           "pricing": [{"billable": "output_image", "unit": unit, "cost_usd": .007}]}],
        }},
    )


def mappings(m, provider_type="openrouter"):
    provider = SimpleNamespace(
        provider_type=provider_type,
        base_url="https://openrouter.ai/api/v1" if provider_type == "openrouter" else "https://generic.example/v1",
        db_id=1, upstream_name=provider_type, provider_fee=1.06,
        get_cached_models=lambda: [m],
    )
    return create_model_mappings([provider], {}, set())


def enable(monkeypatch):
    monkeypatch.setattr(settings, "image_generation_enabled", True)
    monkeypatch.setattr(settings, "image_max_request_usd", .1)


def test_discovered_image_is_not_advertised_while_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, "image_generation_enabled", False)
    _, candidates, public = mappings(model())
    assert "image-model" in candidates
    assert not public


def test_bounded_images_advertised_only_when_enabled(monkeypatch):
    enable(monkeypatch)
    _, candidates, public = mappings(model())
    assert candidates["image-model"]
    assert public["image-model"].api_capabilities["images"]


def test_unbounded_image_model_retained_for_explicit_rejection(monkeypatch):
    enable(monkeypatch)
    _, candidates, public = mappings(model("token"))
    assert candidates["image-model"]
    assert not public


def test_generic_provider_cannot_inherit_images_api_support(monkeypatch):
    enable(monkeypatch)
    _, candidates, public = mappings(model(), "generic")
    assert not candidates
    assert not public


def test_stale_capability_not_advertised(monkeypatch):
    enable(monkeypatch)
    _, _, public = mappings(model(fetched_at=1))
    assert not public


def test_dual_output_chat_catalogue_is_preserved(monkeypatch):
    monkeypatch.setattr(settings, "image_generation_enabled", False)
    _, _, public = mappings(model("token", dual=True))
    assert "image-model" in public
