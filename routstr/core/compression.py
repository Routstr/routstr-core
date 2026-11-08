"""Compressed, revalidatable JSON responses for the model catalog.

``GET /v1/models`` is the largest payload a node serves and the one every
client polls on a timer: each routstrd daemon fetches it for every discovered
node on each refresh pass (every 21 minutes) and gives the request a hard 10 s
deadline. A node with a few hundred models serialises to roughly 670 KB of
JSON, and that is enough to miss the deadline over a high-latency path (a FIPS
mesh route, a Tor circuit, a mobile uplink). A missed fetch is not a cosmetic
problem: the daemon records no freshness stamp for the node, so the node is
listed as ``UNREACHABLE`` even though it answers every other request, and it
drops out of routing until a fetch happens to fit in the budget.

Two response properties fix that without touching the payload:

* ``gzip`` takes this catalog from ~670 KB to ~85 KB. The model metadata is
  extremely repetitive (the same architecture, tokenizer and description
  shapes over and over), so it compresses far better than generic JSON.
* A content-derived ``ETag`` lets a client that already holds the catalog ask
  for it again with ``If-None-Match`` and get a bodyless ``304``.

Compression is negotiated, never imposed. A client that sends no
``Accept-Encoding`` — or ``gzip;q=0`` — still gets the identity body, and every
response carries ``Vary: Accept-Encoding`` so a shared cache cannot hand one
client's encoding to another. Only the catalog routes use this: the proxy's
streaming responses must not be buffered or re-encoded.
"""

from __future__ import annotations

import gzip
import hashlib
from collections import OrderedDict
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

# Below this, compression costs more bytes (gzip header + footer) than it saves
# and the transfer is not what makes a client late. Catalogs are orders of
# magnitude larger; a health check's tiny JSON is not.
MIN_COMPRESSIBLE_BYTES = 1024

# 6 is gzip's default: within ~1% of level 9 on this payload at a fraction of
# the CPU, and the result is cached (see _gzip_cache) so it is paid once per
# catalog revision, not once per request.
_GZIP_LEVEL = 6

# A catalog changes only when prices or providers are refreshed, so one entry
# is the steady state. A handful of slots covers a client that is mid-flight
# across a refresh without letting a long-lived process pin old bodies.
_GZIP_CACHE_MAX_ENTRIES = 8

_gzip_cache: "OrderedDict[str, bytes]" = OrderedDict()


def _quality(header: str, encoding: str) -> float | None:
    """Return the client's q-value for ``encoding``, or None if unspecified.

    An explicit ``encoding;q=`` always wins over ``*;q=``: a client that sends
    ``gzip;q=0, *`` has asked for everything except gzip. Repeated tokens take
    the highest q-value, which is the only reading that cannot disable an
    encoding the client did in fact accept.
    """
    explicit: float | None = None
    wildcard: float | None = None
    for part in header.split(","):
        token, _, parameters = part.partition(";")
        token = token.strip().lower()
        if not token:
            continue
        quality = 1.0
        for parameter in parameters.split(";"):
            name, _, value = parameter.partition("=")
            if name.strip().lower() != "q":
                continue
            try:
                quality = float(value.strip())
            except ValueError:
                quality = 0.0
        if token == encoding:
            explicit = quality if explicit is None else max(explicit, quality)
        elif token == "*":
            wildcard = quality if wildcard is None else max(wildcard, quality)
    return explicit if explicit is not None else wildcard


def accepts_gzip(header: str | None) -> bool:
    """True when ``Accept-Encoding`` permits a gzip response body."""
    if not header:
        return False
    quality = _quality(header, "gzip")
    return quality is not None and quality > 0


def _etag_for(body: bytes) -> str:
    """Weak ETag over the identity body.

    Weak because the same catalog is served in several encodings: the tag
    identifies the data, not the bytes on the wire, which is exactly what
    ``If-None-Match`` compares.
    """
    return f'W/"{hashlib.sha256(body).hexdigest()[:32]}"'


def _opaque(tag: str) -> str:
    tag = tag.strip()
    return tag[2:].strip() if tag.startswith("W/") else tag


def etag_matches(header: str | None, etag: str) -> bool:
    """True when an ``If-None-Match`` header selects ``etag``.

    Uses the weak comparison the RFC prescribes for ``If-None-Match``, so a
    client that echoes the tag without its ``W/`` prefix still revalidates.
    """
    if not header:
        return False
    if header.strip() == "*":
        return True
    wanted = _opaque(etag)
    return any(_opaque(candidate) == wanted for candidate in header.split(","))


def gzipped(etag: str, body: bytes) -> bytes:
    """gzip ``body``, reusing the result for an unchanged catalog.

    Compressing ~670 KB costs a few milliseconds, which is not free on a
    request every node polls on a timer, and the body only changes when the
    catalog does — so the compressed form is kept under its ETag.
    """
    cached = _gzip_cache.get(etag)
    if cached is not None:
        _gzip_cache.move_to_end(etag)
        return cached
    compressed = gzip.compress(body, compresslevel=_GZIP_LEVEL)
    _gzip_cache[etag] = compressed
    while len(_gzip_cache) > _GZIP_CACHE_MAX_ENTRIES:
        _gzip_cache.popitem(last=False)
    return compressed


def clear_compression_cache() -> None:
    """Drop cached compressed bodies. Tests and admin-driven catalog reloads."""
    _gzip_cache.clear()


def negotiated_json_response(payload: Any, request: Request) -> Response:
    """Render ``payload`` as JSON, compressed and revalidatable.

    The body is produced by FastAPI's own encoder and ``JSONResponse``, so the
    bytes a client receives are exactly what the endpoint returned before this
    helper existed — only the encoding and the validators are new.
    """
    body = JSONResponse(content=jsonable_encoder(payload)).body
    etag = _etag_for(body)
    headers = {
        "ETag": etag,
        "Vary": "Accept-Encoding",
        # A client may store the catalog but must revalidate: revalidation is a
        # 304 against a hash, while a heuristic freshness window would serve a
        # stale price list for minutes.
        "Cache-Control": "no-cache",
    }

    if etag_matches(request.headers.get("if-none-match"), etag):
        # 304 carries no body and, per RFC 9110, no representation metadata.
        return Response(status_code=304, headers=headers)

    if len(body) >= MIN_COMPRESSIBLE_BYTES and accepts_gzip(
        request.headers.get("accept-encoding")
    ):
        return Response(
            content=gzipped(etag, body),
            media_type="application/json",
            headers={**headers, "Content-Encoding": "gzip"},
        )

    return Response(content=body, media_type="application/json", headers=headers)
