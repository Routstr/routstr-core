"""Reservation discount from the visible ``X-Tinfoil-Max-Output-Tokens`` header.

A sealed (EHBP) request body is ciphertext, so the completion caps a plaintext
request carries are unreadable and the reservation used to fall back to the
model's whole envelope. Tinfoil's enclave reads this one request header and
enforces it over the body (tinfoilsh/confidential-model-router#518), which makes
it the only completion ceiling the node can quote against without decrypting
anything.

These tests pin the security contract, not just the arithmetic:

* the value must be a single canonical positive integer, or no discount at all;
* only the routes the enclave applies the header on may be discounted;
* only a provider that publishes the header (Tinfoil) may be discounted;
* the unseen input allowance is never discounted, so the hold always covers the
  worst case for an input of unknown size plus the enforced output cap;
* the full reserve is kept whenever any of the above does not hold, and the
  header itself is still forwarded so the enclave stays the rejecting authority;
* billing is unchanged — the reservation is a hold, not a price.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import Headers

from routstr import proxy as proxy_module
from routstr.core.db import ApiKey
from routstr.core.settings import settings
from routstr.payment.helpers import calculate_discounted_max_cost
from routstr.upstream.base import BaseUpstreamProvider
from routstr.upstream.ehbp import (
    _PROXY_ONLY_HEADERS,
    EHBPForwardingTarget,
    _prepare_ehbp_upstream_headers,
)
from routstr.upstream.tinfoil import (
    TinfoilUpstreamProvider,
    _canonical_route,
    _cap_header_would_be_dropped,
    _header_field_values,
    parse_max_output_tokens_header,
)
from routstr.upstream.tinfoil_trailer import TrailerResponse

from .proxy_test_utils import patch_proxy_session

HEADER = "X-Tinfoil-Max-Output-Tokens"


@pytest.fixture
def cap_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the retained scaffolding.

    The shipped default is off: the enforced header is a *per-choice* cap and a
    sealed body can hide ``n``, so no quote is sound yet (see
    ``EHBP_OUTPUT_TOKEN_RESERVATION_ENABLED``). These tests prove the machinery
    is ready and correct for the day the enclave enforces a request total; the
    gate tests below prove it stays inert until then.
    """
    import routstr.upstream.tinfoil as tinfoil_module

    monkeypatch.setattr(tinfoil_module, "EHBP_OUTPUT_TOKEN_RESERVATION_ENABLED", True)


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "8", "1024", str(2**31 - 1), " 8 ", "\t8"])
def test_canonical_values_yield_the_cap(raw: str) -> None:
    assert parse_max_output_tokens_header([raw]) == int(raw.strip())


@pytest.mark.parametrize(
    "values",
    [
        [],
        [""],
        ["   "],
        ["0"],  # the enclave rejects below 1
        ["-1"],
        ["+8"],
        ["8.0"],
        ["8,9"],  # comma-joined list
        ["8 9"],
        ["8, 9"],
        ["08"],  # stricter than the enclave on purpose: over-reserve, never under
        ["0x10"],
        ["1e3"],
        ["eight"],
        ["8\n"],
        ["8", "9"],  # repeated field lines
        [str(2**31)],  # above the enclave's int32 range
        ["9" * 100],  # long enough to blow up int() if it were parsed
    ],
)
def test_non_canonical_values_yield_no_cap(values: list[str]) -> None:
    assert parse_max_output_tokens_header(values) is None


def test_repeated_header_fields_are_no_cap() -> None:
    """Two field lines are a repeated header, which the enclave rejects."""
    headers = Headers(
        raw=[
            (b"x-tinfoil-max-output-tokens", b"8"),
            (b"x-tinfoil-max-output-tokens", b"9"),
        ]
    )
    assert _header_field_values(headers, HEADER) == ["8", "9"]
    assert parse_max_output_tokens_header(_header_field_values(headers, HEADER)) is None


def test_header_lookup_is_case_insensitive_for_plain_mappings() -> None:
    """Upstream code hands headers around as plain dicts; HTTP names are
    case-insensitive, so a differently-cased key must still be found."""
    assert _header_field_values({HEADER: "8"}, HEADER) == ["8"]
    assert _header_field_values({HEADER.lower(): "8"}, HEADER) == ["8"]
    assert _header_field_values({"other": "8"}, HEADER) == []


# ---------------------------------------------------------------------------
# Route and provider applicability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["v1/responses", "responses", "/v1/responses/", "v1/responses?x=1"]
)
def test_cap_applies_on_routes_the_enclave_honors(path: str, cap_enabled: None) -> None:
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    assert provider.enforced_completion_cap({HEADER.lower(): "8"}, path) == 8


@pytest.mark.parametrize("path", ["v1/chat/completions", "/v1/chat/completions/"])
def test_chat_completions_is_never_discounted(path: str, cap_enabled: None) -> None:
    """The enforced cap is *per choice*, so a sealed chat body can bill ``n*cap``.

    Live probe against router-0.tinfoil.sh (deepseek-v4-1-flash, header=8):
    ``n=1`` -> 8 completion tokens, ``n=2`` -> 16, ``n=3`` -> 24, and
    ``header=200, n=2`` -> 400; ``n`` is honoured and the router forwards it
    untouched. Prompt tokens are billed once (52 in every case), but the output
    side is unbounded. ``n``/``best_of`` live inside the sealed body, so no
    server-side check can bound that fan-out: a chat reservation would be
    unbounded and must keep the full envelope until the upstream enforces a
    total. ``best_of`` alone was measured not to multiply (header=8, best_of=3
    -> 8 tokens); ``n`` is what rules the route out.
    """
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    assert provider.enforced_completion_cap({HEADER.lower(): "8"}, path) is None


@pytest.mark.parametrize(
    "path",
    [
        # Routes where the enclave ignores the header, so it cannot bound a bill.
        "v1/completions",  # not served by deployed enclaves; mapping unverified
        "v1/messages",
        "v1/embeddings",
        "v1/models",
        "v1/images/generations",
    ],
)
def test_cap_is_not_applied_on_other_routes(path: str, cap_enabled: None) -> None:
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    assert provider.enforced_completion_cap({HEADER.lower(): "8"}, path) is None


def test_canonical_route_normalization() -> None:
    assert _canonical_route("/v1/chat/completions/") == "chat/completions"
    assert _canonical_route("chat/completions") == "chat/completions"
    assert _canonical_route("v1/responses?x=1") == "responses"
    assert _canonical_route("v1/v1/responses") == "v1/responses"


def test_provider_without_the_header_keeps_full_reserve() -> None:
    """A provider that does not publish such a header must never be discounted."""
    other = BaseUpstreamProvider(base_url="https://example.test", api_key="key")
    assert (
        other.enforced_completion_cap({HEADER.lower(): "8"}, "v1/chat/completions")
        is None
    )


def test_header_is_forwarded_to_the_enclave_verbatim() -> None:
    """The enclave is the authority on the value, so the header must survive.

    Stripping it would either lose the enforced cap (and the client's intent) or
    make the enclave ignore a cap the client relied on.
    """
    assert HEADER.lower() not in _PROXY_ONLY_HEADERS
    forwarded = _prepare_ehbp_upstream_headers(
        {HEADER.lower(): " 8 ", "ehbp-encapsulated-key": "abc"},
        {"X-Tinfoil-Request-Usage-Metrics": "true"},
    )
    assert forwarded[HEADER.lower()] == " 8 "

    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    prepared = provider.prepare_headers({HEADER.lower(): "8"})
    assert prepared[HEADER.lower()] == "8"


# ---------------------------------------------------------------------------
# Reservation math
# ---------------------------------------------------------------------------


def _tinfoil_shaped_model(
    *,
    context_length: int | None = 100_000,
    prompt: float = 0.001,
    completion: float = 0.004,
    top_provider: Any = None,
) -> MagicMock:
    """A model shaped the way ``TinfoilUpstreamProvider.fetch_models`` builds it.

    Tinfoil publishes ``context_window`` and no ``top_provider``, so the base
    envelope is ``context_length * max(prompt, completion)`` — the shape whose
    completion envelope does *not* contain the prompt envelope.
    """
    pricing = MagicMock()
    pricing.prompt = prompt
    pricing.completion = completion
    pricing.max_prompt_cost = (context_length or 0) * prompt
    pricing.max_completion_cost = (context_length or 0) * completion
    pricing.max_cost = (context_length or 0) * max(prompt, completion)

    model = MagicMock()
    model.sats_pricing = pricing
    model.top_provider = top_provider
    model.context_length = context_length
    return model


def _base_msats(model: MagicMock, tol: float = 0.0) -> int:
    return int(model.sats_pricing.max_cost * 1000 * (1 - tol / 100))


async def test_smaller_cap_shrinks_only_the_output_side() -> None:
    """The hold keeps the whole unseen-input allowance and caps the output.

    ``(window - cap) * prompt + cap * completion`` = 99 + 4 sats for a 100k
    window, 0.001/0.004 sats per token and a 1k cap. The input term must be
    present: 99 sats of prompt could bill even though the body is sealed.
    """
    model = _tinfoil_shaped_model()
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            _base_msats(model),
            {},  # EHBP bodies are ciphertext: nothing readable reaches here
            model_obj=model,
            path="v1/chat/completions",
            enforced_completion_cap=1_000,
        )

    assert cost == 103_000
    # The naive "subtract the completion envelope" rule would have reserved only
    # the capped output (4_000 msats) and left the hidden input uncovered.
    assert cost >= 99_000


async def test_no_cap_keeps_the_full_envelope() -> None:
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            base, {}, model_obj=model, path="v1/chat/completions"
        )
    assert cost == base


async def test_cap_at_or_above_the_window_is_no_discount() -> None:
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        for cap in (100_000, 250_000):
            cost = await calculate_discounted_max_cost(
                base,
                {},
                model_obj=model,
                path="v1/chat/completions",
                enforced_completion_cap=cap,
            )
            assert cost == base


async def test_larger_body_cap_wins_over_a_smaller_header() -> None:
    """Largest declared cap bounds the reservation, as on the plaintext path.

    A body that still declares a wider cap (replayed, or a caller that set both)
    must not produce a smaller hold than the body alone would.
    """
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        body_wins = await calculate_discounted_max_cost(
            base,
            {"max_completion_tokens": 50_000},
            model_obj=model,
            path="v1/chat/completions",
            enforced_completion_cap=1_000,
        )
        header_wins = await calculate_discounted_max_cost(
            base,
            {"max_tokens": 100},
            model_obj=model,
            path="v1/chat/completions",
            enforced_completion_cap=1_000,
        )

    # (100_000 - 50_000)*0.001 + 50_000*0.004 = 50 + 200 = 250 sats.
    assert body_wins == 250_000
    # The smaller body cap is overridden by the larger header, as the enclave
    # overrides the body in both directions.
    assert header_wins == 103_000


async def test_input_priced_above_output_keeps_the_whole_window() -> None:
    """With input dearer than output the entire window can bill as hidden input."""
    model = _tinfoil_shaped_model(prompt=0.01, completion=0.001)
    base = _base_msats(model)
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            base,
            {},
            model_obj=model,
            path="v1/chat/completions",
            enforced_completion_cap=1_000,
        )
    assert cost == base  # 1000 sats: all-input case, never below it


async def test_unknown_window_keeps_the_full_envelope() -> None:
    model = _tinfoil_shaped_model(context_length=None)
    base = 400_000
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            base,
            {},
            model_obj=model,
            path="v1/chat/completions",
            enforced_completion_cap=1_000,
        )
    assert cost == base


async def test_unusable_prices_keep_the_full_envelope() -> None:
    model = _tinfoil_shaped_model(prompt=float("nan"))
    base = 400_000
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            base,
            {},
            model_obj=model,
            path="v1/chat/completions",
            enforced_completion_cap=1_000,
        )
    assert cost == base


async def test_tolerance_applies_to_the_capped_worst_case() -> None:
    model = _tinfoil_shaped_model()
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 1.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            _base_msats(model, tol=1.0),
            {},
            model_obj=model,
            path="v1/chat/completions",
            enforced_completion_cap=1_000,
        )
    # ceil(103 sats * 0.99) = ceil(101_970) msats
    assert cost == 101_970


async def test_request_minimum_floor_still_applies() -> None:
    model = _tinfoil_shaped_model(
        context_length=1_000, prompt=0.000001, completion=0.000001
    )
    base = _base_msats(model)
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1_000),
    ):
        cost = await calculate_discounted_max_cost(
            base,
            {},
            model_obj=model,
            path="v1/chat/completions",
            enforced_completion_cap=1,
        )
    assert cost == 1_000


async def test_fixed_pricing_ignores_the_cap() -> None:
    model = _tinfoil_shaped_model()
    with patch.object(settings, "fixed_pricing", True):
        cost = await calculate_discounted_max_cost(
            7_000,
            {},
            model_obj=model,
            path="v1/chat/completions",
            enforced_completion_cap=1_000,
        )
    assert cost == 7_000


# ---------------------------------------------------------------------------
# Proxy wiring: which candidates may be discounted
# ---------------------------------------------------------------------------


def test_plaintext_requests_keep_body_derived_pricing(cap_enabled: None) -> None:
    """Plaintext bodies are readable, so the header is not consulted there."""
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    request = MagicMock()
    request.headers = Headers(raw=[(b"x-tinfoil-max-output-tokens", b"8")])

    assert (
        proxy_module._enforced_completion_cap(
            False,
            candidate_upstream=provider,
            request=request,
            path="v1/chat/completions",
        )
        is None
    )
    assert (
        proxy_module._enforced_completion_cap(
            True,
            candidate_upstream=provider,
            request=request,
            path="v1/responses",
        )
        == 8
    )


def test_non_tinfoil_candidate_is_never_discounted() -> None:
    other = BaseUpstreamProvider(base_url="https://example.test", api_key="key")
    request = MagicMock()
    request.headers = Headers(raw=[(b"x-tinfoil-max-output-tokens", b"8")])
    assert (
        proxy_module._enforced_completion_cap(
            True,
            candidate_upstream=other,
            request=request,
            path="v1/chat/completions",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Proxy end-to-end: the amount actually reserved
# ---------------------------------------------------------------------------


def _encrypted_error_response() -> TrailerResponse:
    """A sealed upstream error, so the flow settles without a live enclave."""
    body = b"\x00sealed-error"
    return TrailerResponse(
        status_code=400,
        headers=[("content-length", str(len(body))), ("ehbp-response-nonce", "n1")],
        body=body,
        trailers=[],
    )


async def _run_ehbp_request(
    *,
    path: str,
    header_values: list[str] | None,
    base_msats: int,
    model_obj: MagicMock,
    fallback_upstream: Any = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[Any, list[int], list[dict[str, str]]]:
    """Drive ``proxy.proxy`` for a sealed request.

    Returns the response, every amount ``pay_for_request`` reserved (in order,
    so a failover re-reserve is visible) and the headers each upstream forward
    carried.
    """
    key = ApiKey(hashed_key="ehbpkey", balance=10_000_000)
    session = MagicMock()

    raw = [
        (b"authorization", b"Bearer sk-ehbpkey"),
        (b"ehbp-encapsulated-key", b"abc123"),
        (b"x-routstr-model", b"tinfoil/test-model"),
    ]
    for value in header_values or []:
        raw.append((b"x-tinfoil-max-output-tokens", value.encode()))
    raw.extend(extra_headers or [])

    request = MagicMock()
    request.method = "POST"
    request.headers = Headers(raw=raw)
    request.query_params = {}
    request.body = AsyncMock(return_value=b"sealed-body")

    upstream = TinfoilUpstreamProvider(api_key="upstream-key")
    candidates = [(model_obj, upstream)]
    if fallback_upstream is not None:
        candidates.append((model_obj, fallback_upstream))

    reserved: list[int] = []

    async def capture(_key: Any, amount: int, _session: Any) -> Any:
        reserved.append(amount)
        return MagicMock()

    forwarded: list[dict[str, str]] = []

    async def forward_with_trailer(**kwargs: Any) -> TrailerResponse:
        forwarded.append(dict(kwargs.get("headers") or {}))
        return _encrypted_error_response()

    with (
        patch.object(proxy_module, "get_candidates", return_value=candidates),
        patch.object(
            proxy_module, "get_max_cost_for_model", AsyncMock(return_value=base_msats)
        ),
        patch.object(proxy_module, "check_token_balance", MagicMock()),
        patch.object(proxy_module, "get_bearer_token_key", AsyncMock(return_value=key)),
        patch.object(proxy_module, "pay_for_request", AsyncMock(side_effect=capture)),
        patch.object(proxy_module, "revert_pay_for_request", AsyncMock()),
        patch(
            "routstr.upstream.ehbp.forward_with_trailer",
            AsyncMock(side_effect=forward_with_trailer),
        ),
        patch_proxy_session(session),
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        response = await proxy_module.proxy(request, path)

    return response, reserved, forwarded


async def test_proxy_reserves_the_capped_amount_for_a_sealed_request(
    cap_enabled: None,
) -> None:
    model = _tinfoil_shaped_model()
    response, reserved, _ = await _run_ehbp_request(
        path="v1/responses",
        header_values=["1000"],
        base_msats=_base_msats(model),
        model_obj=model,
    )
    # (100_000 - 1_000)*0.001 + 1_000*0.004 = 103 sats
    assert reserved == [103_000]
    assert response.status_code == 400


async def test_proxy_reserves_the_full_envelope_without_the_header() -> None:
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    _, reserved, _ = await _run_ehbp_request(
        path="v1/responses",
        header_values=None,
        base_msats=base,
        model_obj=model,
    )
    assert reserved == [base]


async def test_proxy_forwards_the_header_unchanged_to_the_enclave(
    cap_enabled: None,
) -> None:
    """The cap is enforced by the enclave, so it must reach it untouched."""
    model = _tinfoil_shaped_model()
    _, _, forwarded = await _run_ehbp_request(
        path="v1/responses",
        header_values=["1000"],
        base_msats=_base_msats(model),
        model_obj=model,
    )
    assert len(forwarded) == 1
    sent = {key.lower(): value for key, value in forwarded[0].items()}
    assert sent["x-tinfoil-max-output-tokens"] == "1000"
    # The enclave is asked for usage metrics so billing stays usage-based.
    assert sent["x-tinfoil-request-usage-metrics"] == "true"


async def test_proxy_ignores_a_malformed_header_and_still_forwards_it(
    cap_enabled: None,
) -> None:
    """No quote is taken from a value the enclave might reject; the header is
    not rewritten either, so the enclave still answers with its own 400."""
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    _, reserved, forwarded = await _run_ehbp_request(
        path="v1/responses",
        header_values=["8,9"],
        base_msats=base,
        model_obj=model,
    )
    assert reserved == [base]
    sent = {key.lower(): value for key, value in forwarded[0].items()}
    assert sent["x-tinfoil-max-output-tokens"] == "8,9"


async def test_proxy_ignores_the_header_on_a_route_the_enclave_ignores() -> None:
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    _, reserved, _ = await _run_ehbp_request(
        path="v1/messages",
        header_values=["1000"],
        base_msats=base,
        model_obj=model,
    )
    assert reserved == [base]


async def test_proxy_keeps_the_full_envelope_on_chat_completions(
    cap_enabled: None,
) -> None:
    """The per-choice fan-out hazard is the route's, not the header's.

    A sealed chat body can carry ``n`` (measured: ``n=2, header=8`` bills 16
    completion tokens), so the same header that quotes on ``/v1/responses``
    must not shrink a chat reservation. The header is still forwarded — the
    enclave enforces it — but the node keeps the full envelope.
    """
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    _, reserved, _ = await _run_ehbp_request(
        path="v1/chat/completions",
        header_values=["1000"],
        base_msats=base,
        model_obj=model,
    )
    assert reserved == [base]


async def test_proxy_ignores_a_repeated_header() -> None:
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    _, reserved, _ = await _run_ehbp_request(
        path="v1/responses",
        header_values=["8", "100"],
        base_msats=base,
        model_obj=model,
    )
    assert reserved == [base]


class _OtherEhbpProvider(BaseUpstreamProvider):
    """A sealed-body provider that publishes no enforced output cap.

    Tinfoil is the only EHBP provider today; this stands in for the next one, so
    the failover safeguard is pinned before it can regress. It inherits the base
    ``enforced_completion_cap``, which is what a provider without the header
    does.
    """

    provider_type = "other-ehbp"
    supports_ehbp = True

    def get_ehbp_forwarding_target(self, path: str, model_obj: Any) -> Any:
        return EHBPForwardingTarget(url="https://other.test/v1/chat/completions")


async def test_failover_re_reserves_at_the_fallbacks_full_envelope(
    cap_enabled: None,
) -> None:
    """A fallback that ignores the header cannot inherit the Tinfoil quote.

    The first candidate is discounted and fails; the second publishes no
    enforced cap, so it is re-reserved at its full envelope before it is tried.
    """
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    fallback = _OtherEhbpProvider(base_url="https://other.test", api_key="k")

    _, reserved, _ = await _run_ehbp_request(
        path="v1/responses",
        header_values=["1000"],
        base_msats=base,
        model_obj=model,
        fallback_upstream=fallback,
    )
    # The 400 from the first candidate is retryable, so the loop re-reserves
    # (it only ever grows) before trying the fallback.
    assert reserved[0] == 103_000
    assert reserved[-1] == base


async def test_plaintext_body_discount_is_unchanged() -> None:
    """Pin the plaintext path: the header is not consulted and body caps still
    discount exactly as before this change."""
    pricing = MagicMock()
    pricing.prompt = 0.001
    pricing.completion = 0.004
    pricing.max_prompt_cost = 128_000 * 0.001
    pricing.max_completion_cost = 16_384 * 0.004
    pricing.max_cost = (128_000 - 16_384) * 0.001 + 16_384 * 0.004

    top_provider = MagicMock()
    top_provider.context_length = 128_000
    top_provider.max_completion_tokens = 16_384

    model = MagicMock()
    model.sats_pricing = pricing
    model.top_provider = top_provider
    model.context_length = 128_000

    base = int(pricing.max_cost * 1000)
    body = {"model": "m", "max_completion_tokens": 1_000}
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            base, body, model_obj=model, path="v1/chat/completions"
        )

    # 177_152 - 111_607 (prompt allowance, 9 estimated prompt tokens) -
    # 61_536 (completion allowance for the 1_000-token cap).
    assert base == 177_152
    assert cost == 4_009


# ---------------------------------------------------------------------------
# Forwarding sanitizer: a Connection nomination must invalidate the quote
# ---------------------------------------------------------------------------


def test_connection_nomination_drops_the_cap(cap_enabled: None) -> None:
    """``Connection`` naming the cap strips it from the outgoing request.

    ``forward_with_trailer`` removes every header a ``Connection`` field
    nominates, so quoting against one would size the hold to a cap the enclave
    never receives — under-reserved output with no bound. The hook must refuse.
    """
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    headers = Headers(
        raw=[
            (b"x-tinfoil-max-output-tokens", b"8"),
            (b"connection", b"X-Tinfoil-Max-Output-Tokens"),
        ]
    )
    assert _cap_header_would_be_dropped(headers) is True
    assert provider.enforced_completion_cap(headers, "v1/responses") is None


def test_connection_nomination_is_case_insensitive(cap_enabled: None) -> None:
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    headers = Headers(
        raw=[
            (b"x-tinfoil-max-output-tokens", b"8"),
            (b"connection", b"keep-alive, x-tinfoil-max-output-tokens"),
        ]
    )
    assert provider.enforced_completion_cap(headers, "v1/responses") is None


def test_unrelated_connection_tokens_keep_the_cap(cap_enabled: None) -> None:
    """Only a nomination of the cap itself disqualifies the quote."""
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    headers = Headers(
        raw=[
            (b"x-tinfoil-max-output-tokens", b"8"),
            (b"connection", b"keep-alive, x-custom-hop"),
        ]
    )
    assert _cap_header_would_be_dropped(headers) is False
    assert provider.enforced_completion_cap(headers, "v1/responses") == 8


def test_cap_header_without_connection_nomination_survives(cap_enabled: None) -> None:
    """The plain case still quotes: the real sanitizer keeps the header."""
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    headers = Headers(raw=[(b"x-tinfoil-max-output-tokens", b"8")])
    assert _cap_header_would_be_dropped(headers) is False
    assert provider.enforced_completion_cap(headers, "v1/responses") == 8


async def test_connection_nominated_cap_keeps_the_full_envelope(
    cap_enabled: None,
) -> None:
    """End-to-end: the refused quote leaves the full reserve in place.

    The forwarding mock captures headers *before* the real sanitizer runs (the
    sanitizer lives inside ``forward_with_trailer``), so this case is also why a
    mock-only regression cannot prove the contract: the quote is refused by the
    provider hook, and the reserve stays at the envelope.
    """
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    _, reserved, _ = await _run_ehbp_request(
        path="v1/responses",
        header_values=["1000"],
        base_msats=base,
        model_obj=model,
        extra_headers=[(b"connection", b"X-Tinfoil-Max-Output-Tokens")],
    )
    assert reserved == [base]


# ---------------------------------------------------------------------------
# Token window: context length only, never a completion-only limit
# ---------------------------------------------------------------------------


async def test_window_ignores_completion_only_top_provider_limit(
    cap_enabled: None,
) -> None:
    """``top_provider.max_completion_tokens`` is the output share, not the window.

    A 100k-context model that also publishes a 10k completion cap must reserve
    against 100k: using 10k as the window would quote 13_000 msats while a valid
    30k-token prompt plus the 1k output costs 34_000 msats, far under the 40_000
    envelope the model actually allows.
    """
    top_provider = MagicMock()
    top_provider.context_length = None
    top_provider.max_completion_tokens = 10_000
    model = _tinfoil_shaped_model(top_provider=top_provider)

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            _base_msats(model),
            {},
            model_obj=model,
            path="v1/responses",
            enforced_completion_cap=1_000,
        )

    # (100_000 - 1_000)*0.001 + 1_000*0.004 = 103 sats, not 13 sats.
    assert cost == 103_000


async def test_window_falls_back_to_top_provider_context_length(
    cap_enabled: None,
) -> None:
    top_provider = MagicMock()
    top_provider.context_length = 50_000
    top_provider.max_completion_tokens = 10_000
    model = _tinfoil_shaped_model(context_length=100_000, top_provider=top_provider)

    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            _base_msats(model),
            {},
            model_obj=model,
            path="v1/responses",
            enforced_completion_cap=1_000,
        )

    # (50_000 - 1_000)*0.001 + 1_000*0.004 = 53 sats.
    assert cost == 53_000


# ---------------------------------------------------------------------------
# Capability advertisement
# ---------------------------------------------------------------------------


async def test_info_omits_the_capability_by_default() -> None:
    """No feature key while the discount is inactive.

    A client that saw the key would fund a capped hold this node does not
    shrink, and pay a 402 for a reservation it cannot cover.
    """
    from routstr.core.main import info

    payload = await info()
    assert "features" not in payload


async def test_info_advertises_routes_and_total_cap_when_enabled(
    cap_enabled: None,
) -> None:
    """The enabled shape, kept ready for the day the upstream guarantee lands."""
    from routstr.core.main import info

    payload = await info()
    features = payload["features"]
    assert features["ehbp_output_token_reservation"] is True
    assert features["ehbp_output_token_reservation_total_cap"] is True
    # Authoritative for the booleans: only fan-out-free routes may be quoted.
    assert features["ehbp_output_token_reservation_routes"] == ["responses"]


# ---------------------------------------------------------------------------
# Fail-closed gate: the shipped default must not discount anything
# ---------------------------------------------------------------------------


def test_no_cap_is_quoted_by_default() -> None:
    """A valid header on the best route still quotes nothing while disabled."""
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    assert (
        provider.enforced_completion_cap({HEADER.lower(): "8"}, "v1/responses") is None
    )


def test_proxy_never_reaches_the_discount_by_default() -> None:
    provider = TinfoilUpstreamProvider(api_key="upstream-key")
    request = MagicMock()
    request.headers = Headers(raw=[(b"x-tinfoil-max-output-tokens", b"1000")])
    assert (
        proxy_module._enforced_completion_cap(
            True,
            candidate_upstream=provider,
            request=request,
            path="v1/responses",
        )
        is None
    )


async def test_proxy_reserves_the_full_envelope_by_default() -> None:
    """End-to-end fail-closed: a sealed request with a valid header is unchanged.

    This is the exploit shape the gate exists for — a direct caller sending a cap
    in the visible header while the sealed body hides ``n``. Reserving the full
    envelope is what the node did before this change, so the default cannot be
    cheaper than the status quo.
    """
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    _, reserved, _ = await _run_ehbp_request(
        path="v1/responses",
        header_values=["1"],
        base_msats=base,
        model_obj=model,
    )
    assert reserved == [base]


async def test_calculate_discounted_max_cost_still_requires_an_explicit_cap() -> None:
    """The math is inert without an enforced cap: the hook is the only source."""
    model = _tinfoil_shaped_model()
    base = _base_msats(model)
    with (
        patch.object(settings, "fixed_pricing", False),
        patch.object(settings, "tolerance_percentage", 0.0),
        patch.object(settings, "min_request_msat", 1),
    ):
        cost = await calculate_discounted_max_cost(
            base, {}, model_obj=model, path="v1/responses"
        )
    assert cost == base
