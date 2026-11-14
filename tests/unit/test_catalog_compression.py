"""Negotiated compression and revalidation for the model catalog endpoints.

The catalog is what every routstrd daemon polls on a 21-minute timer with a
10 s deadline, so these tests pin the two properties that keep that poll
cheap: the body is gzipped only when the client asked for it, and an unchanged
catalog costs a 304 instead of 670 KB.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request as StarletteRequest

from routstr.core import compression
from routstr.core.compression import (
    MIN_COMPRESSIBLE_BYTES,
    accepts_gzip,
    clear_compression_cache,
    etag_matches,
    negotiated_json_response,
)
from routstr.core.db import get_session

CATALOG: dict[str, Any] = {
    "data": [
        {
            "id": f"vendor/model-{index}",
            "name": f"Vendor Model {index}",
            "created": 0,
            "description": "A model served by a local inference proxy",
            "context_length": 8192,
            "architecture": {
                "modality": "text->text",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tokenizer": "Other",
                "instruct_type": None,
            },
            "pricing": {"prompt": 0.0000001, "completion": 0.0000002},
        }
        for index in range(300)
    ]
}


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    clear_compression_cache()


def make_request(**headers: str) -> StarletteRequest:
    """A bare GET request carrying just the headers under test."""
    return StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/models",
            "query_string": b"",
            "headers": [
                (name.replace("_", "-").encode(), value.encode())
                for name, value in headers.items()
            ],
        }
    )


def identity_body(payload: Any) -> bytes:
    """The exact bytes FastAPI's JSONResponse produces for ``payload``."""
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


class TestAcceptEncoding:
    @pytest.mark.parametrize(
        "header",
        ["gzip", "GZIP", "br, gzip", "gzip;q=0.5", "gzip, deflate, br", "*", "*;q=0.1"],
    )
    def test_permits_gzip(self, header: str) -> None:
        assert accepts_gzip(header) is True

    @pytest.mark.parametrize(
        "header",
        [None, "", "identity", "br", "deflate", "gzip;q=0", "gzip;q=0.0", "*;q=0"],
    )
    def test_refuses_gzip(self, header: str | None) -> None:
        assert accepts_gzip(header) is False

    def test_explicit_zero_beats_wildcard(self) -> None:
        # "everything except gzip" is a real client preference (already-gzipped
        # upstreams, or a client that stores its own compressed copy).
        assert accepts_gzip("gzip;q=0, *") is False


class TestEtag:
    def test_matches_its_own_tag(self) -> None:
        assert etag_matches('W/"abc"', 'W/"abc"') is True

    def test_matches_without_weak_prefix(self) -> None:
        assert etag_matches('"abc"', 'W/"abc"') is True

    def test_matches_in_a_list(self) -> None:
        assert etag_matches('"other", W/"abc"', 'W/"abc"') is True

    def test_matches_wildcard(self) -> None:
        assert etag_matches("*", 'W/"abc"') is True

    def test_rejects_a_stale_tag(self) -> None:
        assert etag_matches('W/"old"', 'W/"abc"') is False

    def test_absent_header_never_matches(self) -> None:
        assert etag_matches(None, 'W/"abc"') is False
        assert etag_matches("", 'W/"abc"') is False


class TestNegotiatedJsonResponse:
    def test_gzips_when_accepted(self) -> None:
        response = negotiated_json_response(
            CATALOG, make_request(accept_encoding="gzip")
        )

        assert response.headers["content-encoding"] == "gzip"
        assert response.headers["vary"].lower() == "accept-encoding"
        assert response.headers["etag"].startswith('W/"')
        assert int(response.headers["content-length"]) == len(response.body)
        assert gzip.decompress(response.body) == identity_body(CATALOG)

    def test_gzip_is_worth_it_for_a_catalog(self) -> None:
        # The property the timeout fix depends on: a real catalog is highly
        # repetitive, so the wire body is a fraction of the identity body.
        response = negotiated_json_response(
            CATALOG, make_request(accept_encoding="gzip")
        )
        ratio = len(identity_body(CATALOG)) / len(response.body)
        assert ratio > 5, f"catalog compressed only {ratio:.1f}x"

    def test_identity_when_client_does_not_ask(self) -> None:
        response = negotiated_json_response(
            CATALOG, make_request(accept_encoding="identity")
        )

        assert "content-encoding" not in response.headers
        assert response.headers["vary"].lower() == "accept-encoding"
        assert response.body == identity_body(CATALOG)

    def test_identity_without_accept_encoding(self) -> None:
        response = negotiated_json_response(CATALOG, make_request())

        assert "content-encoding" not in response.headers
        assert response.body == identity_body(CATALOG)

    def test_small_payloads_are_not_compressed(self) -> None:
        payload = {"data": [{"id": "tiny"}]}
        assert len(identity_body(payload)) < MIN_COMPRESSIBLE_BYTES

        response = negotiated_json_response(
            payload, make_request(accept_encoding="gzip")
        )

        assert "content-encoding" not in response.headers
        # Revalidation still works; only the encoding is skipped.
        assert response.headers["etag"].startswith('W/"')

    def test_304_for_a_matching_tag(self) -> None:
        first = negotiated_json_response(CATALOG, make_request(accept_encoding="gzip"))
        etag = first.headers["etag"]

        revalidated = negotiated_json_response(
            CATALOG, make_request(accept_encoding="gzip", if_none_match=etag)
        )

        assert revalidated.status_code == 304
        assert revalidated.body == b""
        assert revalidated.headers["etag"] == etag
        assert revalidated.headers["vary"].lower() == "accept-encoding"
        assert "content-encoding" not in revalidated.headers

    def test_304_for_wildcard(self) -> None:
        response = negotiated_json_response(
            CATALOG, make_request(accept_encoding="gzip", if_none_match="*")
        )

        assert response.status_code == 304

    def test_200_for_a_stale_tag(self) -> None:
        response = negotiated_json_response(
            CATALOG, make_request(accept_encoding="gzip", if_none_match='W/"stale"')
        )

        assert response.status_code == 200
        assert gzip.decompress(response.body) == identity_body(CATALOG)

    def test_etag_follows_the_catalog(self) -> None:
        before = negotiated_json_response(CATALOG, make_request())
        changed = {"data": CATALOG["data"] + [{"id": "vendor/new-model"}]}
        after = negotiated_json_response(changed, make_request())

        assert before.headers["etag"] != after.headers["etag"]

    def test_etag_is_stable_for_unchanged_catalog(self) -> None:
        # Same data, different dict instance and insertion order of top-level
        # keys: the tag must not churn, or every poll revalidates to a 200.
        reordered = {"data": CATALOG["data"]}
        first = negotiated_json_response(CATALOG, make_request())
        second = negotiated_json_response(reordered, make_request())

        assert first.headers["etag"] == second.headers["etag"]

    def test_compressed_body_is_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []
        real_compress = gzip.compress

        def counting_compress(data: bytes, **kwargs: Any) -> bytes:
            calls.append(len(data))
            return real_compress(data, **kwargs)

        monkeypatch.setattr(compression.gzip, "compress", counting_compress)

        for _ in range(3):
            negotiated_json_response(CATALOG, make_request(accept_encoding="gzip"))

        assert len(calls) == 1

    def test_compressed_cache_is_bounded(self) -> None:
        for index in range(compression._GZIP_CACHE_MAX_ENTRIES + 4):
            payload = {"data": [{"id": f"vendor/model-{index}"} for _ in range(200)]}
            negotiated_json_response(payload, make_request(accept_encoding="gzip"))

        assert len(compression._gzip_cache) <= compression._GZIP_CACHE_MAX_ENTRIES


class _FakeModel:
    """Stands in for ``payment.models.Model`` without touching the database."""

    def __init__(self, index: int) -> None:
        self.index = index

    def dict(self) -> dict[str, Any]:
        return {"id": f"vendor/model-{self.index}", "name": f"Model {self.index}"}


@pytest.fixture
def catalog_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """The real ``/v1/models`` route, with the catalog and DB session stubbed."""
    from routstr import proxy
    from routstr.payment.models import models_router

    monkeypatch.setattr(
        proxy, "get_unique_models", lambda: [_FakeModel(i) for i in range(300)]
    )

    app = FastAPI()
    app.include_router(models_router)
    app.dependency_overrides[get_session] = lambda: None
    return app


@pytest.mark.asyncio
async def test_models_endpoint_negotiates_gzip_and_revalidates(
    catalog_app: FastAPI,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=catalog_app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models", headers={"accept-encoding": "gzip"})

        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        assert response.headers["vary"].lower() == "accept-encoding"
        assert len(response.json()["data"]) == 300

        revalidated = await client.get(
            "/v1/models",
            headers={
                "accept-encoding": "gzip",
                "if-none-match": response.headers["etag"],
            },
        )

        assert revalidated.status_code == 304
        assert revalidated.content == b""


@pytest.mark.asyncio
async def test_models_endpoint_serves_identity_to_plain_clients(
    catalog_app: FastAPI,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=catalog_app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/models", headers={"accept-encoding": "identity"}
        )

        assert response.status_code == 200
        assert "content-encoding" not in response.headers
        # Same JSON shape the endpoint has always returned.
        assert response.json() == {
            "data": [
                {"id": f"vendor/model-{i}", "name": f"Model {i}"} for i in range(300)
            ]
        }


@pytest.mark.asyncio
async def test_models_endpoint_response_is_a_response_object(
    catalog_app: FastAPI,
) -> None:
    # Guards the endpoint signature: returning the helper's Response (not a
    # dict) is what makes FastAPI skip its own serialization, so a refactor
    # back to a plain dict would silently drop compression.
    async with AsyncClient(
        transport=ASGITransport(app=catalog_app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models", headers={"accept-encoding": "gzip"})

        assert isinstance(response.headers["etag"], str)
        assert response.headers["cache-control"] == "no-cache"
