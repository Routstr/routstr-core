"""Count-aware scaffolding; mocks do not prove deployed enclave enforcement."""

from typing import Any

import pytest
from starlette.datastructures import Headers

from routstr.upstream import tinfoil
from routstr.upstream.ehbp import _prepare_ehbp_upstream_headers
from routstr.upstream.tinfoil_trailer import _strip_hop_by_hop_headers

from .test_tinfoil_max_output_tokens_reservation import (
    _base_msats,
    _OtherEhbpProvider,
    _run_ehbp_request,
    _tinfoil_shaped_model,
)

CAP = b"x-tinfoil-max-output-tokens"
COUNT = b"x-tinfoil-completion-count"
VALID = [(CAP, b"1000"), (COUNT, b"1")]


@pytest.fixture
def single_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tinfoil, "EHBP_SINGLE_CHOICE_OUTPUT_TOKEN_RESERVATION_ENABLED", True
    )


def quote(raw: list[tuple[bytes, bytes]], path: str = "v1/chat/completions") -> Any:
    return tinfoil.TinfoilUpstreamProvider("test").enforced_completion_cap(
        Headers(raw=raw), path
    )


@pytest.mark.parametrize(
    "path",
    [
        "chat/completions",
        "v1/chat/completions",
        "/v1/chat/completions",
        "v1/chat/completions?x=1",
    ],
)
def test_both_headers_quote_only_with_new_gate(path: str, single_enabled: None) -> None:
    assert quote(VALID, path) == 1000


@pytest.mark.parametrize(
    "path", ["v1/chat/completions/", "v1/responses", "v1/completions", "v1/messages"]
)
def test_new_gate_does_not_widen_other_routes(path: str, single_enabled: None) -> None:
    assert quote(VALID, path) is None


@pytest.mark.parametrize("field", [CAP, COUNT])
@pytest.mark.parametrize(
    "values",
    [
        [],
        [b""],
        [b"0"],
        [b"-1"],
        [b"+1"],
        [b"1.0"],
        [b"01"],
        [b"1,1"],
        [b"1", b"1"],
        [b"1", b"2"],
        [b"9" * 100],
    ],
)
async def test_missing_invalid_or_duplicate_header_keeps_full_envelope(
    field: bytes, values: list[bytes], single_enabled: None
) -> None:
    headers = [(key, value) for key, value in VALID if key != field]
    headers += [(field, value) for value in values]
    assert quote(headers) is None
    model = _tinfoil_shaped_model()
    _, reserved, _ = await _run_ehbp_request(
        path="v1/chat/completions",
        header_values=None,
        base_msats=_base_msats(model),
        model_obj=model,
        extra_headers=headers,
    )
    assert reserved == [_base_msats(model)]


@pytest.mark.parametrize("count", [b"2", b"1000", b"2147483647"])
def test_multiple_choices_keep_full_envelope(
    count: bytes, single_enabled: None
) -> None:
    assert quote([(CAP, b"1000"), (COUNT, count)]) is None


@pytest.mark.parametrize("field", [CAP, COUNT])
async def test_connection_nominating_either_header_refuses_quote(
    field: bytes, single_enabled: None
) -> None:
    # Inspect every raw Connection value, not just the value a dict retains.
    headers = VALID + [(b"connection", field.upper()), (b"connection", b"keep-alive")]
    assert quote(headers) is None
    model = _tinfoil_shaped_model()
    _, reserved, _ = await _run_ehbp_request(
        path="v1/chat/completions",
        header_values=None,
        extra_headers=headers,
        base_msats=_base_msats(model),
        model_obj=model,
    )
    assert reserved == [_base_msats(model)]
    assert field.decode() not in _strip_hop_by_hop_headers(
        {field.decode(): "1", "connection": field.decode()}
    )


@pytest.mark.parametrize("field", [CAP, COUNT])
def test_real_sanitizer_removal_invalidates_quote(
    field: bytes, single_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tinfoil._strip_hop_by_hop_headers

    def dropping(headers: dict[str, str]) -> dict[str, str]:
        return {
            key: value
            for key, value in original(headers).items()
            if key.lower() != field.decode()
        }

    monkeypatch.setattr(tinfoil, "_strip_hop_by_hop_headers", dropping)
    assert quote(VALID) is None


def test_both_headers_survive_real_preparation_even_while_disabled() -> None:
    provider = tinfoil.TinfoilUpstreamProvider("test")
    headers = {CAP.decode(): " 1000 ", COUNT.decode(): "1"}
    sent = _strip_hop_by_hop_headers(
        _prepare_ehbp_upstream_headers(
            provider.prepare_headers(headers),
            {"X-Tinfoil-Request-Usage-Metrics": "true"},
            provider.confidential_inference_profile,
        )
    )
    for key, value in headers.items():
        assert sent[key] == value


@pytest.mark.parametrize("enabled", [False, True])
async def test_direct_opaque_body_cannot_assert_a_single_choice(
    enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tinfoil, "EHBP_SINGLE_CHOICE_OUTPUT_TOKEN_RESERVATION_ENABLED", enabled
    )
    model = _tinfoil_shaped_model()
    # An opaque stand-in for ciphertext hiding malicious n/best_of. Core must
    # never infer count from it. No count header means full, even with cap=1.
    _, reserved, _ = await _run_ehbp_request(
        path="v1/chat/completions",
        header_values=["1"],
        base_msats=_base_msats(model),
        model_obj=model,
        sealed_body=b"\x00opaque-hidden-n-and-best-of",
    )
    assert reserved == [_base_msats(model)]


async def test_default_with_both_valid_headers_stays_full() -> None:
    assert quote(VALID) is None
    model = _tinfoil_shaped_model()
    _, reserved, forwarded = await _run_ehbp_request(
        path="v1/chat/completions",
        header_values=None,
        extra_headers=VALID,
        base_msats=_base_msats(model),
        model_obj=model,
    )
    assert reserved == [_base_msats(model)]
    sent = {key.lower(): value for key, value in forwarded[0].items()}
    assert sent[COUNT.decode()] == "1"
    assert sent[CAP.decode()] == "1000"


async def test_chat_scaffold_reserves_per_candidate(single_enabled: None) -> None:
    model = _tinfoil_shaped_model()
    fallback = _OtherEhbpProvider(base_url="https://other.test", api_key="test")
    _, reserved, _ = await _run_ehbp_request(
        path="v1/chat/completions",
        header_values=None,
        extra_headers=VALID,
        base_msats=_base_msats(model),
        model_obj=model,
        fallback_upstream=fallback,
    )
    assert reserved == [103_000, _base_msats(model)]


@pytest.mark.parametrize("legacy", [False, True])
async def test_info_contract_is_separate_from_legacy_flags(
    legacy: bool, single_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routstr.core.main import info

    monkeypatch.setattr(tinfoil, "EHBP_OUTPUT_TOKEN_RESERVATION_ENABLED", legacy)
    features = (await info())["features"]
    assert features["ehbp_single_choice_output_token_reservation"] is True
    assert "chat/completions" in features["ehbp_output_token_reservation_routes"]
    assert features.get("ehbp_output_token_reservation_total_cap", False) is legacy
    assert features["ehbp_output_token_reservation_routes"] == (
        ["chat/completions", "responses"] if legacy else ["chat/completions"]
    )
