"""Which models a confidential node offers for chat: only text output, and
the operator's ``chat_model_filter`` from the sidecar offer
(``exclude_if_supports``, ``exclude_if_priced``).

The request body is opaque to the node, which bills the provider-reported
prompt and completion tokens at the model's token rates. A model that can
also produce images or audio, accepts a cost-raising parameter the chat
suffix cannot pin off, or has a priced component the node does not bill
(tiered prompt prices, per-search fees) could be made to cost the node more
than it charges, so it is not offered.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import routstr.confidential as conf
from routstr.payment.models import Model

HOST = "openrouter.ai"


class _Up:
    def __init__(self, base_url: str = "https://openrouter.ai/api/v1") -> None:
        self.base_url = base_url
        self.provider_type = "openrouter"
        self.provider_fee = 1.0


def _raw(
    mid: str,
    out: list[str] | None = None,
    params: list[str] | None = None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An OpenRouter catalogue row (the fields ``Model`` reads)."""
    row: dict[str, Any] = {
        "id": mid,
        "name": mid,
        "created": 1,
        "description": "",
        "context_length": 1000,
        "architecture": {
            "modality": "text->text",
            "input_modalities": ["text"],
            "output_modalities": out or ["text"],
            "tokenizer": "x",
            "instruct_type": None,
        },
        "pricing": pricing or {"prompt": "0.000001", "completion": "0.000002"},
    }
    if params is not None:
        row["supported_parameters"] = params
    return row


def _offered(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
    side_extra: dict[str, Any] | None = None,
) -> set[str]:
    import routstr.proxy as proxy

    models = [Model(**r) for r in rows]
    by_id = {m.id: m for m in models}
    up = _Up()
    monkeypatch.setattr(proxy, "get_unique_models", lambda: models)
    monkeypatch.setattr(proxy, "get_candidates", lambda mid: [(by_id[mid], up)])
    side = {"upstream_host": HOST, **(side_extra or {})}
    return set(conf.offered_models(side))


def test_only_text_output_models_are_offered_for_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model with image, audio or video output is not offered: a client
    could ask for it in the private part (``modalities``) and the provider
    would bill those tokens above the text rate the node charges."""
    rows = [
        _raw("text"),
        _raw("image-and-text", ["image", "text"]),
        _raw("text-and-image", ["text", "image"]),
        _raw("text-and-audio", ["text", "audio"]),
        _raw("text-and-video", ["text", "video"]),
        _raw("image", ["image"]),
    ]
    assert _offered(monkeypatch, rows) == {"text"}


def test_the_providers_own_row_must_be_text_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listed model and the provider row the sidecar host bills by can
    differ; both must produce text only."""
    import routstr.proxy as proxy

    listed = Model(**_raw("m"))
    provider_row = Model(**_raw("m", ["image", "text"]))
    monkeypatch.setattr(proxy, "get_unique_models", lambda: [listed])
    monkeypatch.setattr(proxy, "get_candidates", lambda mid: [(provider_row, _Up())])
    assert conf.offered_models({"upstream_host": HOST}) == {}


def test_exclude_if_supports(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model whose catalogue lists a parameter named in
    ``exclude_if_supports`` is not offered; without the filter it is. A model
    whose catalogue lists no parameters is not offered under the filter
    (fail closed)."""
    rows = [
        _raw("plain", params=["max_tokens", "tools"]),
        _raw("search", params=["max_tokens", "web_search_options"]),
        _raw("unlisted"),
    ]
    assert _offered(monkeypatch, rows) == {"plain", "search", "unlisted"}
    flt = {"chat_model_filter": {"exclude_if_supports": ["web_search_options"]}}
    assert _offered(monkeypatch, rows, flt) == {"plain"}
    assert (
        conf.filtered_out(Model(**rows[1]), {"web_search_options"}, set())
        == "supports web_search_options"
    )
    assert (
        conf.filtered_out(Model(**rows[2]), {"web_search_options"}, set())
        == "supported_parameters unknown"
    )


def test_exclude_if_priced(monkeypatch: pytest.MonkeyPatch) -> None:
    """``exclude_if_priced`` names pricing components: a ``Pricing`` rate
    above zero (``web_search``, ``request``) or a member only the catalogue
    has (``overrides`` tiers, ``audio``, ``input_cache_write_1h``)."""
    base = {"prompt": "0.000001", "completion": "0.000002"}
    rows = [
        _raw("plain", pricing=dict(base, input_cache_read="0.0000001")),
        _raw("zero-search", pricing=dict(base, web_search="0")),
        _raw("search-fee", pricing=dict(base, web_search="0.01")),
        _raw(
            "tiered",
            pricing=dict(
                base, overrides=[{"min_prompt_tokens": 100000, "prompt": "0.000005"}]
            ),
        ),
        _raw("no-tiers", pricing=dict(base, overrides=[])),
        _raw("cache-1h", pricing=dict(base, input_cache_write_1h="0.000002")),
    ]
    assert len(_offered(monkeypatch, rows)) == 6
    flt = {
        "chat_model_filter": {
            "exclude_if_priced": ["web_search", "overrides", "input_cache_write_1h"]
        }
    }
    assert _offered(monkeypatch, rows, flt) == {"plain", "zero-search", "no-tiers"}
    tiered = Model(**rows[3])
    assert tiered.extra_pricing_keys == ["overrides"]
    assert conf.filtered_out(tiered, set(), {"overrides"}) == "priced overrides"


def test_catalogue_fields_survive_a_snapshot_and_stay_absent_otherwise() -> None:
    """The two catalogue fields round-trip through ``Model.dict()`` (the
    model snapshot) and are left out of it when the catalogue has none, so a
    provider without them (Venice) serialises as before."""
    row = _raw(
        "m",
        params=["tools", "web_search_options"],
        pricing={
            "prompt": "0.000001",
            "completion": "0.000002",
            "audio": "0.000003",
            "overrides": [{"min_prompt_tokens": 5, "prompt": "0.1"}],
        },
    )
    m = Model(**row)
    assert m.supported_parameters == ["tools", "web_search_options"]
    assert m.extra_pricing_keys == ["audio", "overrides"]
    d = m.dict()
    assert d["supported_parameters"] == ["tools", "web_search_options"]
    again = Model(**d)
    assert again.extra_pricing_keys == ["audio", "overrides"]
    assert again.supported_parameters == m.supported_parameters
    plain = Model(**_raw("p")).dict()
    assert "supported_parameters" not in plain and "extra_pricing_keys" not in plain


@pytest.mark.parametrize(
    "block",
    [
        "web_search_options",
        ["web_search_options"],
        {"exclude_if_support": ["x"]},
        {"exclude_if_supports": "web_search_options"},
        {"exclude_if_supports": [1]},
        {"exclude_if_priced": ["bad key"]},
    ],
)
def test_a_malformed_filter_refuses_the_offer(block: Any) -> None:
    """A typo must not silently offer every model."""
    side = {"head_template": "", "chat_model_filter": block}
    with pytest.raises(conf.OfferRefused):
        conf.chat_model_filter(side)
    with pytest.raises(conf.OfferRefused):
        conf.validate_sidecar_offer(side)


def test_no_filter_block_filters_nothing() -> None:
    assert conf.chat_model_filter({}) == (set(), set())
    assert conf.chat_model_filter({"chat_model_filter": {}}) == (set(), set())
    assert conf.filtered_out(SimpleNamespace(), set(), set()) is None
