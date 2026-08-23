"""Unit tests for the proxy edge path allowlist (arbitrary-upstream-path-proxy).

An authenticated POST used to be forwarded for ANY path, so a caller could reach
arbitrary or traversal-shaped upstream endpoints with the provider credential
attached. The proxy now rejects ambiguous path spellings for every method, then
requires the method/path pair to name a canonical endpoint. A familiar prefix is
no longer enough: "v1/organization/api_keys" is refused just like "internal/admin".
"""

from __future__ import annotations

import os

os.environ.setdefault("UPSTREAM_BASE_URL", "http://test")
os.environ.setdefault("UPSTREAM_API_KEY", "test")

import pytest  # noqa: E402

from routstr.proxy import (  # noqa: E402
    _forwarding_allowed,
    _is_ambiguously_spelled_path,
    _parse_extra_allowed_endpoints,
)


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "v1/../admin",
        "v1/./models",
        "..",
        "v1//models",  # duplicate separator
        "/v1/models",  # leading slash / absolute override
        "v1/models/..",
        "%2e%2e/secret",  # residual encoded dot segment
        "v1/%2fadmin",  # residual encoded slash
        "v1\\models",  # backslash
        "v1/models\x00",  # NUL byte
        " v1/models",  # leading whitespace
        "",
    ],
)
def test_ambiguous_paths_are_rejected(path: str) -> None:
    assert _is_ambiguously_spelled_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "v1/chat/completions",
        "chat/completions",
        "v1/responses",
        "v1/embeddings",
        "models",
        "v1/models/gpt-4",
        "attestation/",  # a single trailing slash is canonical
        "tee/attestation/",
    ],
)
def test_canonical_paths_are_allowed(path: str) -> None:
    assert _is_ambiguously_spelled_path(path) is False


def test_unknown_paths_are_not_forwarded() -> None:
    # The credential is attached during forwarding, so an unknown endpoint must
    # never be forwarded on the caller's say-so.
    assert _forwarding_allowed("internal/admin", "POST") is False
    assert _forwarding_allowed("secret-endpoint", "POST") is False


@pytest.mark.parametrize(
    "path",
    [
        "modelsdump",  # "models" must not match a longer segment
        "attestationadmin",
        "providers-secret",
        "embeddingsx",
        "completions-internal",
    ],
)
def test_endpoint_name_does_not_match_a_longer_segment(path: str) -> None:
    assert _forwarding_allowed(path, "POST") is False
    assert _forwarding_allowed(path, "GET") is False


@pytest.mark.parametrize(
    "path",
    [
        # A familiar prefix must not carry an unknown endpoint. These are real
        # upstream routes that manage keys, org membership, and billing.
        "v1/organization/api_keys",
        "v1/api_keys",
        "v1/admin/keys",
        "v1/billing/usage",
        "v1/files",
        "v1/batches",
        "chat/internal",
        "audio/internal",
        "images/internal",
        "tee/keys",
        # No endpoint takes a trailing id segment; a resource id never widens
        # the reachable surface.
        "models/gpt-4",
        "models/gpt-4/secret",
        "chat/completions/abc",
    ],
)
def test_known_prefix_does_not_carry_an_unknown_endpoint(path: str) -> None:
    assert _forwarding_allowed(path, "POST") is False
    assert _forwarding_allowed(path, "GET") is False


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("v1/chat/completions", "POST"),
        ("chat/completions", "POST"),
        ("v1/chat/completions/", "POST"),
        ("completions", "POST"),
        ("v1/responses", "POST"),
        ("v1/messages", "POST"),
        ("v1/embeddings", "POST"),
        ("moderations", "POST"),
        ("audio/transcriptions", "POST"),
        ("images/generations", "POST"),
        ("models", "GET"),
        ("attestation", "GET"),
        ("tee/attestation", "GET"),
    ],
)
def test_canonical_endpoints_are_forwarded(path: str, method: str) -> None:
    assert _forwarding_allowed(path, method) is True


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("chat/completions", "GET"),  # billed endpoints are POST-only
        ("v1/embeddings", "GET"),
        ("models", "POST"),  # read-only endpoints are GET-only
        ("attestation", "POST"),
        ("v1/chat/completions", "DELETE"),  # never routed here, refused anyway
        ("v1/chat/completions", "PUT"),
    ],
)
def test_method_must_match_the_endpoint(path: str, method: str) -> None:
    assert _forwarding_allowed(path, method) is False


def test_operator_additions_are_parsed_per_endpoint() -> None:
    parsed = _parse_extra_allowed_endpoints("POST:v1/rerank, GET:batches ,post:audio/x")
    assert parsed == {
        "rerank": frozenset({"POST"}),  # the "v1/" prefix collapses like any path
        "batches": frozenset({"GET"}),
        "audio/x": frozenset({"POST"}),
    }


def test_operator_additions_may_grant_two_methods_on_one_endpoint() -> None:
    assert _parse_extra_allowed_endpoints("POST:batches,GET:batches") == {
        "batches": frozenset({"POST", "GET"})
    }


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "v1/rerank",  # no method
        "POST:",  # no path
        ":v1/rerank",  # empty method
        "DELETE:v1/rerank",  # method the proxy never routes
        "POST:*",  # wildcards are deliberately unsupported
        "POST:v1/*",
        "POST:../secret",  # ambiguous spellings are screened here too
        "POST:v1//rerank",
        "POST:%2e%2e/secret",
    ],
)
def test_malformed_operator_additions_widen_nothing(raw: str) -> None:
    assert _parse_extra_allowed_endpoints(raw) == {}


def test_operator_additions_are_env_only() -> None:
    # The proxy parses this once at import, so a persisted or admin-API-writable
    # value would be read but never take effect. Keeping it env-only also means
    # widening the reachable upstream surface takes a deploy.
    from routstr.core.settings import ENV_ONLY_FIELDS

    assert "proxy_extra_allowed_paths" in ENV_ONLY_FIELDS


def test_ehbp_is_gated_by_the_same_allowlist() -> None:
    # EHBP hides the request body from the proxy, which is a reason to constrain
    # the destination more tightly rather than to trust the caller's path: the
    # encrypted contract covers the body, never the endpoint the provider
    # credential is spent against.
    assert _forwarding_allowed("anything/encrypted", "POST") is False
    assert _forwarding_allowed("v1/organization/api_keys", "POST") is False
    assert _forwarding_allowed("v1/chat/completions", "POST") is True
