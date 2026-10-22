"""Confidential-upstream mode: the node's offer and billing glue.

In this mode the client is the TLS 1.3 client of the upstream provider through
the node: a separate cu-sidecar relays the TLS records, writes the one record
that carries the provider API key and verifies the zero-knowledge proofs. The
node never sees the prompt or the response.

Billing reuses the node's normal path, like the EHBP/Tinfoil opaque-body flow:
the client authenticates with an ordinary bearer (``sk-`` key or Cashu token),
the node reserves the full-context prompt plus the pinned completion cap with
``pay_for_request``, charges the π_C3S-verified usage at the rates frozen at
setup (the signed offer's) and releases the reservation when the upstream
never ran. Refunds of unused balance are the existing ``/v1/wallet/refund``.

Optional pricing for the node's own work (all off by default):
``CONFIDENTIAL_RATE_SURCHARGE_PCT`` raises the per-token rates of the signed
offer; ``CONFIDENTIAL_SESSION_FEE_MSAT`` is a flat fee charged on top of the
usage of a completed request (and for a provider refusal, 4xx/503, which also
reached the provider); ``CONFIDENTIAL_ABANDON_FEE_MSAT`` (≥ the session fee,
defaulting to it) is charged instead when a session the sidecar accepted ends
without a request reaching the provider (an armed, pre-warmed session never
used, an abort before the body, a client gone): the anti-DoS price of the
~22 MB / ~0.5 s CPU the node spends per session. The reservation includes the
larger of the two. A client accepts both by echoing the signed offer's
``session_fee_msat`` and ``abandon_fee_msat`` in its setup; a setup that does
not is refused, so an older client is never charged a fee it does not check.

Image generation (``kind: "images"``) is offered when the sidecar's offer has
an ``endpoints.images`` block: the signed offer lists, per image model, the
variants it can price (pinned tier and format) at a flat msat per image. The
client's suffix pins model, count and variant, π_C2S proves it, and the charge
is that flat price × count plus the session fee, fixed at setup. A success
needs no disclosure (the sidecar reports ``usage.flat``); a provider error
uses the same error-head disclosure as chat.

``CONFIDENTIAL_RESERVE_MARGIN_TOKENS`` (default 16) completion tokens are
reserved beyond the pinned cap, because a provider may bill a few more than
``max_tokens`` and the charge is capped at the reservation. The margin only
enlarges the hold (it is in the signed offer, so a client can size its
balance); the charge is still the usage the provider reported.

Inert unless ``CONFIDENTIAL_SIDECAR_URL`` is set.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException

from .core import get_logger
from .core.settings import settings

logger = get_logger(__name__)

# Wire-protocol version; the offer's min_v/max_v is the only negotiation. Only
# protocol v2 (the two-party handshake) is supported: v1 is retired.
CONFIDENTIAL_VERSION = 2
CONFIDENTIAL_MIN_V = 2
CONFIDENTIAL_MAX_V = 2

# Images per confidential image request (the count the suffix pins), and the
# only output format offered: WebP is the smallest response.
IMAGE_MAX_COUNT = 4
IMAGE_IDLE_RANGE = (30, 600)
IMAGE_FORMAT = "webp"
DEFAULT_IMAGE_COUNT_KEY = "variants"

# Upstream HTTP errors answered before any inference starts, so the request
# itself is not billed once the error head is disclosed under π_C3S.
UNBILLED_ERROR_STATUSES = frozenset(range(400, 500)) | {503}
# Of those, the ones the provider's or the node's own state caused: only the
# session fee. That is rate limiting and capacity (429, 503), and the node's
# provider account (401, 403: the key the node wrote is missing, revoked or
# not entitled; 402: its credit ran out), which only the operator can fix.
# Every other 4xx is a request the provider rejected after the node did the
# session's 2PC, so the client pays the abandon fee, as for a session it
# never used; otherwise a deliberately invalid body would be a free way to
# make the node work.
PROVIDER_REFUSAL_STATUSES = frozenset({401, 402, 403, 429, 503})

# A 4K image takes the provider long enough (a busy one took 44 s and 5.9 MB as
# base64) that several in one response could outlast the provider's idle
# limits or the response cap: such variants are offered one image at a time.
IMAGE_MAX_COUNT_BY_RESOLUTION = {"4K": 1}

# The offer fetch sits on the unauthenticated ``/v1/models`` path: keep it short,
# remember failures briefly and serve the last good offer while it is not stale
# (the sidecar offer is static for one sidecar run).
_SIDECAR_TIMEOUT = httpx.Timeout(3.0)
_SIDECAR_OFFER_TTL_S = 30.0
_SIDECAR_OFFER_STALE_S = 600.0
_SIDECAR_FAILURE_TTL_S = 10.0


def session_fee_msat() -> int:
    """The configured flat fee per confidential session (msat, ≥ 0)."""
    try:
        return max(0, int(settings.confidential_session_fee_msat or 0))
    except (TypeError, ValueError):
        return 0


def abandon_fee_msat() -> int:
    """The fee (msat) for a session that ends without a request reaching the
    provider: the configured abandon fee, never below the session fee (and
    equal to it when unset)."""
    try:
        fee = max(0, int(settings.confidential_abandon_fee_msat or 0))
    except (TypeError, ValueError):
        fee = 0
    return max(fee, session_fee_msat())


def reserve_margin_tokens() -> int:
    """Completion tokens reserved beyond the pinned cap (≥ 0)."""
    try:
        return max(0, int(settings.confidential_reserve_margin_tokens or 0))
    except (TypeError, ValueError):
        return 0


def rate_surcharge_pct() -> float:
    """The configured surcharge on per-token rates (percent, ≥ 0)."""
    try:
        pct = float(settings.confidential_rate_surcharge_pct or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return pct if math.isfinite(pct) and pct > 0 else 0.0


def surcharged(entry: dict[str, float], pct: float) -> dict[str, float]:
    """``entry``'s rates raised by ``pct`` percent (unchanged at 0)."""
    if pct <= 0:
        return dict(entry)
    factor = 1.0 + pct / 100.0
    return {k: float(v) * factor for k, v in entry.items()}


def pricing_fields(fee_msat: int, abandon_msat: int, pct: float) -> dict[str, Any]:
    """The offer/receipt fields for the confidential pricing; empty when off,
    so a node with the defaults signs exactly the payload it signed before."""
    out: dict[str, Any] = {}
    if fee_msat > 0:
        out["session_fee_msat"] = int(fee_msat)
    if abandon_msat > 0:
        out["abandon_fee_msat"] = int(abandon_msat)
    if pct > 0:
        out["rate_surcharge_pct"] = pct
    return out


# A signed offer stays valid for setup (``offer_sig``) this long after it was
# last served (not after it was signed); at most this many are kept.
SIGNED_OFFER_TTL_S = 600.0
SIGNED_OFFER_MAX = 512


def to_ws_url(url: str) -> str:
    """``http``→``ws`` and ``https``→``wss``; only the scheme changes."""
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urlunsplit(parts._replace(scheme=scheme))


def warn_unprotected_sidecar() -> None:
    """Log when the sidecar is reached off loopback without a token: the
    sidecar refuses such a node, and anyone who can reach it spends the key."""
    host = (urlsplit(settings.confidential_sidecar_url).hostname or "").lower()
    loopback = host == "localhost" or host == "::1" or host.startswith("127.")
    if not loopback and not settings.confidential_sidecar_token:
        logger.error(
            "confidential: CONFIDENTIAL_SIDECAR_URL is not on loopback but "
            "CONFIDENTIAL_SIDECAR_TOKEN is empty; the sidecar requires the token"
        )
    elif not loopback:
        # The sidecar serves plain HTTP: the token (which lets its holder
        # spend the provider key) crosses this hop unencrypted.
        logger.warning(
            "confidential: the sidecar is not on loopback; its token is sent "
            "unencrypted, so the network to it must be private (a container "
            "network, a WireGuard or SSH tunnel)"
        )


def sidecar_headers() -> dict[str, str]:
    """The node's credential for the sidecar (empty when no token is set)."""
    token = settings.confidential_sidecar_token
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------------------
# Signing (node Nostr key)
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON for signatures (sorted keys, no whitespace)."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def node_pubkey_hex() -> str | None:
    from .nostr.listing import nsec_to_keypair

    kp = nsec_to_keypair((settings.nsec or "").strip()) if settings.nsec else None
    return kp[1] if kp else None


def sign_payload(payload: Any) -> str | None:
    """BIP-340 Schnorr over ``sha256(canonical_json(payload))`` with the node key."""
    from nostr_sdk import Keys

    from .nostr.listing import nsec_to_keypair

    kp = nsec_to_keypair((settings.nsec or "").strip()) if settings.nsec else None
    if kp is None:
        return None
    digest = hashlib.sha256(canonical_json(payload)).digest()
    return Keys.parse(kp[0]).sign_schnorr(digest)


# ---------------------------------------------------------------------------
# Sidecar offer and model selection
# ---------------------------------------------------------------------------


class OfferError(Exception):
    """The node cannot assemble an offer (sidecar unreachable or malformed)."""


class OfferRefused(OfferError):
    """The sidecar answered, but with an offer this node must not serve
    (malformed, not settling through the node, another protocol version).
    Unlike an unreachable sidecar, this drops the last good offer at once."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_sidecar_offer(data: Any) -> None:
    """Shape checks on the fields the node reads from the sidecar offer."""
    if not isinstance(data, dict) or not isinstance(data.get("head_template"), str):
        raise OfferRefused("sidecar offer is malformed")
    cap = data.get("max_tokens_cap", 4096)
    if not _is_int(cap) or cap < 1:
        raise OfferRefused("sidecar offer is malformed: max_tokens_cap")
    key_length = data.get("key_length", 0)
    if not _is_int(key_length) or key_length < 0:
        raise OfferRefused("sidecar offer is malformed: key_length")
    model_allowlist(data)  # raises OfferRefused when malformed


async def fetch_sidecar_offer() -> dict[str, Any]:
    base = settings.confidential_sidecar_url.rstrip("/")
    if not base:
        raise OfferRefused("confidential sidecar is not configured")
    async with httpx.AsyncClient(timeout=_SIDECAR_TIMEOUT) as client:
        resp = await client.get(f"{base}/offer", headers=sidecar_headers())
        resp.raise_for_status()
        data = resp.json()
    validate_sidecar_offer(data)
    version = sidecar_protocol_version(data)
    if version != CONFIDENTIAL_VERSION:
        # A sidecar running the retired protocol v1: offering it would hand
        # clients a version they refuse (and this node no longer relays).
        logger.error(
            "confidential: the sidecar speaks protocol v%s; only v%s is supported "
            "(v1 is retired); confidential mode is disabled",
            version,
            CONFIDENTIAL_VERSION,
        )
        raise OfferRefused(
            f"the confidential sidecar speaks protocol v{version}; "
            f"this node supports only v{CONFIDENTIAL_VERSION}"
        )
    if data.get("node_settles") is not True:
        # The sidecar would sign stub receipts itself and never send the
        # ``usage`` frame this node settles on: every session would end
        # "released, no disclosure" and be charged its full reservation.
        # Refuse to offer confidential mode rather than take that money. Every
        # sidecar that speaks protocol v2 and settles through the node says
        # so explicitly; anything but ``true`` is refused.
        logger.error(
            "confidential: the sidecar runs without --node-settles; "
            "confidential mode is disabled until it does"
        )
        raise OfferRefused("the confidential sidecar does not run with --node-settles")
    return data


_SIDECAR_CACHE: dict[str, Any] = {"at": 0.0, "offer": None, "failed_at": None}


async def cached_sidecar_offer() -> dict[str, Any]:
    """Cached sidecar offer: 30 s fresh, failures remembered for 10 s.

    When a refresh fails, the last good offer is served for up to 10 minutes
    (sessions still fail cleanly at the sidecar dial if it is really gone).
    A sidecar that answers with an offer the node refuses (``OfferRefused``)
    drops the last good offer immediately: it is up, but must not be used.
    """
    now = time.monotonic()
    offer = _SIDECAR_CACHE["offer"]
    age = now - _SIDECAR_CACHE["at"]
    if offer is not None and age < _SIDECAR_OFFER_TTL_S:
        return dict(offer)
    failed_at = _SIDECAR_CACHE["failed_at"]
    if failed_at is None or now - failed_at >= _SIDECAR_FAILURE_TTL_S:
        try:
            offer = await fetch_sidecar_offer()
        except (OfferError, httpx.HTTPError, ValueError) as exc:
            _SIDECAR_CACHE["failed_at"] = now
            if isinstance(exc, OfferRefused):
                _SIDECAR_CACHE["offer"] = offer = None
            logger.warning("confidential: sidecar offer fetch failed: %s", exc)
        else:
            _SIDECAR_CACHE.update(offer=offer, at=now, failed_at=None)
            return dict(offer)
    if offer is not None and age < _SIDECAR_OFFER_STALE_S:
        return dict(offer)
    raise OfferError("confidential sidecar unavailable")


def model_allowlist(side_offer: dict[str, Any]) -> set[str]:
    """The operator's optional allowlist from the sidecar offer (empty = all).

    ``model_allowlist`` is a list of ids; the earlier ``text_only_ok`` map
    (``{id: true}``) is still read. Models and prices otherwise come from the
    node itself: the sidecar offer carries no prices. Anything else (a map
    value that is not a boolean, a list entry that is not a string, another
    type) raises ``OfferRefused``: a typo must not silently allow every model.
    """
    allow = side_offer.get("model_allowlist")
    if allow is None:
        allow = side_offer.get("text_only_ok")
    if allow is None:
        return set()
    if isinstance(allow, dict) and all(isinstance(v, bool) for v in allow.values()):
        return {str(k) for k, v in allow.items() if v}
    if isinstance(allow, list) and all(isinstance(k, str) for k in allow):
        return set(allow)
    raise OfferRefused("sidecar offer is malformed: model_allowlist")


def model_allowed(model: str, allowlist: set[str]) -> bool:
    return not allowlist or model in allowlist


@dataclass(frozen=True)
class Candidate:
    """The (model, provider) pair that serves ``model_id`` via the sidecar host."""

    model: Any
    upstream: Any


def candidate_for(model_id: str, host: str) -> Candidate | None:
    """The ranked candidate whose provider host is the sidecar's upstream host.

    Pricing and settlement must use the provider the sidecar actually talks
    to, not merely the best-ranked candidate for the model id.
    """
    from .proxy import get_candidates

    for model_obj, upstream in get_candidates(model_id) or []:
        if getattr(upstream, "provider_type", "") == "routstr":
            continue
        if urlsplit(upstream.base_url).hostname == host:
            return Candidate(model_obj, upstream)
    return None


def produces_text(model: Any) -> bool:
    """A chat model: confidential mode serves chat completions only.

    Image and embedding models carry zero token rates, which are usable
    prices, so without this they would be offered for chat at no charge.
    """
    arch = getattr(model, "architecture", None)
    return "text" in (getattr(arch, "output_modalities", None) or [])


def offered_models(side_offer: dict[str, Any]) -> dict[str, Candidate]:
    """Models the node serves confidentially: chat models on the sidecar host,
    within the allowlist."""
    from .proxy import get_unique_models

    host = str(side_offer.get("upstream_host", ""))
    allowlist = model_allowlist(side_offer)
    out: dict[str, Candidate] = {}
    for model in get_unique_models():
        if not model_allowed(model.id, allowlist) or not produces_text(model):
            continue
        cand = candidate_for(model.id, host)
        if cand is not None:
            out[model.id] = cand
    return out


def compact_json(value: Any) -> str:
    """JSON as the sidecar and the clients render suffix values: no
    whitespace, non-ASCII kept (``serde_json::to_string``/``JSON.stringify``)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def render_image_suffix(
    model: str,
    count_key: str,
    count: int,
    fixed_pins: list[Any],
    pins: list[Any],
) -> str:
    """The pinned end of an image request body, byte-exact: model, count, the
    endpoint's fixed pins, then the variant's pins, closing the object."""
    parts = [
        '"model":' + compact_json(model),
        compact_json(count_key) + ":" + compact_json(count),
    ]
    parts += [compact_json(k) + ":" + compact_json(v) for k, v in [*fixed_pins, *pins]]
    return "," + ",".join(parts) + "}"


def images_endpoint(side_offer: dict[str, Any]) -> dict[str, Any] | None:
    """The sidecar's ``endpoints.images`` block when it is usable, else ``None``.

    A malformed block disables images only; chat is unaffected.
    """
    endpoints = side_offer.get("endpoints")
    ep = endpoints.get("images") if isinstance(endpoints, dict) else None
    if ep is None:
        return None
    problem = _images_endpoint_problem(ep)
    if problem:
        logger.warning("confidential: sidecar images endpoint ignored: %s", problem)
        return None
    return ep


# Pin keys the clients refuse: under a negating name ``false`` switches a
# feature on, and pinning a protection can only switch it off.
_NEGATING_PIN_PREFIXES = (
    "disable",
    "no_",
    "skip",
    "without",
    "exclude",
    "omit",
    "hide",
)
_PROTECTION_PIN_WORDS = ("safe", "moderat", "filter", "nsfw", "censor", "guard")


def pin_key_problem(key: str) -> str | None:
    low = key.lower()
    if low.startswith(_NEGATING_PIN_PREFIXES):
        return f"{key!r} has a negating name"
    if any(word in low for word in _PROTECTION_PIN_WORDS):
        return f"{key!r} names a protection"
    return None


def _images_endpoint_problem(ep: Any) -> str | None:
    if not isinstance(ep, dict):
        return "not an object"
    if not isinstance(ep.get("head_template"), str) or not ep["head_template"]:
        return "head_template"
    if ep.get("billing", "flat") != "flat":
        return "billing must be flat"
    count_key = ep.get("count_key", DEFAULT_IMAGE_COUNT_KEY)
    if not isinstance(count_key, str) or not count_key:
        return "count_key"
    pins = ep.get("fixed_pins", [])
    if not isinstance(pins, list) or not all(
        isinstance(p, list) and len(p) == 2 and isinstance(p[0], str) for p in pins
    ):
        return "fixed_pins"
    for key, value in pins:
        # The clients' pin policy: a fixed pin only switches an extra off,
        # so a block they would refuse is never signed.
        problem = pin_key_problem(key)
        if problem:
            return f"fixed_pins: {problem}"
        if value is not False and value != []:
            return f"fixed_pins: {key!r} must be false or []"
    space = ep.get("param_space", {})
    if not isinstance(space, dict) or not all(
        isinstance(k, str) and isinstance(v, list) for k, v in space.items()
    ):
        return "param_space"
    keys = ["model", count_key, *(p[0] for p in pins)]
    if len(set(keys)) != len(keys) or set(keys) & set(space):
        return "a pinned key repeats"
    # Numbers the clients check: one out of range must disable images, not
    # make every client refuse the whole signed offer.
    idle = ep.get("response_idle_s", 300)
    if not _is_int(idle) or not IMAGE_IDLE_RANGE[0] <= idle <= IMAGE_IDLE_RANGE[1]:
        return "response_idle_s"
    count = ep.get("max_count", IMAGE_MAX_COUNT)
    if not _is_int(count) or count < 1:
        return "max_count"
    size = ep.get("max_response_bytes")
    if size is not None and (not _is_int(size) or size <= 0):
        return "max_response_bytes"
    return None


def _axis_values(declared: list[str], tiers: list[Any], name: str) -> list[str]:
    """Every value of a price axis: the declared ones, then any a tier names.

    A book whose tiers vary along an axis it does not declare would otherwise
    leave that axis unpinned, and the client could pick the dearer tier
    privately.
    """
    values = list(declared)
    for tier in tiers:
        value = getattr(tier, name, None)
        if value and value not in values:
            values.append(value)
    return values


def image_variants(model_obj: Any, space: dict[str, Any], pct: float) -> list[dict]:
    """The variants of ``model_obj`` the node can price at a flat msat per image.

    One per declared resolution (and quality), always WebP. Only per-image
    books qualify: a token-metered or upstream-reported price depends on the
    response, which the node never sees. A book with a tier axis the endpoint
    cannot pin would let the client pick a dearer tier privately, so it is
    not offered.
    """
    from .payment.image_pricing import per_image_sats

    book = getattr(model_obj, "image_pricing", None)
    if (
        book is None
        or book.unit != "image"
        or book.trust_upstream_cost
        or book.endpoints
        or book.input_image_usd > 0
        or IMAGE_FORMAT not in (space.get("format") or [])
    ):
        return []
    axes: list[list[tuple[str, str] | None]] = []
    for name, declared in (
        ("resolution", _axis_values(book.resolutions, book.tiers, "resolution")),
        ("quality", _axis_values(book.qualities, book.tiers, "quality")),
    ):
        if not declared:
            axes.append([None])
            continue
        allowed = space.get(name)
        if not isinstance(allowed, list):
            return []
        values = [v for v in declared if v in allowed]
        if not values:
            return []
        axes.append([(name, v) for v in values])
    variants = []
    for res in axes[0]:
        for qual in axes[1]:
            chosen = [p for p in (res, qual) if p is not None]
            sats = per_image_sats(model_obj, dict(chosen))
            msat = sats * 1000.0 * (1.0 + pct / 100.0)
            if not math.isfinite(msat) or msat <= 0:
                continue
            variant: dict[str, Any] = {
                "id": "-".join([v for _k, v in chosen] + [IMAGE_FORMAT]),
                "pins": [[k, v] for k, v in chosen] + [["format", IMAGE_FORMAT]],
                # Rounded first: float noise must not add a millisat.
                "msat_per_image": math.ceil(round(msat, 3)),
            }
            cap = IMAGE_MAX_COUNT_BY_RESOLUTION.get(dict(chosen).get("resolution", ""))
            if cap is not None:
                variant["max_count"] = cap
            variants.append(variant)
    return variants


def priced_image_models(
    side_offer: dict[str, Any], pct: float
) -> dict[str, tuple[Candidate, list[dict]]]:
    """Image models on the sidecar host, within the allowlist, with their
    priced variants; empty unless the sidecar offers the images endpoint."""
    from .payment.image_pricing import produces_images
    from .proxy import get_unique_models

    ep = images_endpoint(side_offer)
    if ep is None:
        return {}
    host = str(side_offer.get("upstream_host", ""))
    allowlist = model_allowlist(side_offer)
    space = ep.get("param_space", {})
    out: dict[str, tuple[Candidate, list[dict]]] = {}
    for model in get_unique_models():
        if not model_allowed(model.id, allowlist) or not produces_images(model):
            continue
        # Text to image only: inpaint and upscale need an input image.
        arch = getattr(model, "architecture", None)
        if getattr(arch, "input_modalities", None) != ["text"]:
            continue
        cand = candidate_for(model.id, host)
        if cand is None:
            continue
        # The suffix pins the provider's own spelling: the node's public ids
        # are lower-cased, and a provider may not accept that (Venice
        # answers 404 for a mis-cased model).
        upstream_id = upstream_model_id(cand.model)
        if upstream_id is None:
            continue
        try:
            variants = image_variants(cand.model, space, pct)
        except Exception:
            logger.debug("confidential: no image pricing", extra={"model": model.id})
            continue
        if variants:
            out[upstream_id] = (cand, variants)
    return out


def upstream_model_id(model_obj: Any) -> str | None:
    """The id the provider itself serves the model under, as the plain path
    forwards it; ``None`` if it cannot be determined."""
    mid = getattr(model_obj, "forwarded_model_id", None) or getattr(
        model_obj, "id", None
    )
    return mid if isinstance(mid, str) and mid else None


def images_offer_block(side_offer: dict[str, Any], pct: float) -> dict[str, Any] | None:
    """The ``images`` block of the signed offer, or ``None`` without images."""
    ep = images_endpoint(side_offer)
    if ep is None:
        return None
    priced = priced_image_models(side_offer, pct)
    if not priced:
        return None
    block: dict[str, Any] = {
        "head_template": ep["head_template"],
        "response_shape": str(ep.get("response_shape", "venice")),
        "response_idle_s": ep.get("response_idle_s", 300),
        "count_key": ep.get("count_key", DEFAULT_IMAGE_COUNT_KEY),
        "fixed_pins": ep.get("fixed_pins", []),
        "param_space": ep.get("param_space", {}),
        # The sidecar refuses a larger count, so never sign one it would.
        "max_count": min(ep.get("max_count", IMAGE_MAX_COUNT), IMAGE_MAX_COUNT),
        "price_list": {mid: variants for mid, (_c, variants) in priced.items()},
    }
    if _is_int(ep.get("max_response_bytes")):
        block["max_response_bytes"] = ep["max_response_bytes"]
    return block


def price_entry(model_id: str, cand: Candidate) -> dict[str, float] | None:
    """msats per 1k tokens, from the same rate function billing uses."""
    from .payment.cost_calculation import _get_pricing_rates

    rates = _get_pricing_rates(
        {"model": model_id}, cand.model, getattr(cand.upstream, "provider_fee", None)
    )
    if rates is None:  # fixed-pricing deployment
        in_rate = float(settings.fixed_per_1k_input_tokens) * 1000.0
        out_rate = float(settings.fixed_per_1k_output_tokens) * 1000.0
        return {
            "in": in_rate,
            "cached_in": in_rate,
            "cache_write": in_rate,
            "out": out_rate,
        }
    in_rate, out_rate, cache_read_rate, cache_write_rate = rates
    return {
        "in": float(in_rate),
        "cached_in": float(cache_read_rate),
        "cache_write": float(cache_write_rate),
        "out": float(out_rate),
    }


def usable_price_entry(model_id: str, cand: Candidate) -> dict[str, float] | None:
    """``price_entry`` when every rate in it can be billed on, else ``None``."""
    from .payment.rates import is_usable_rate

    try:
        entry = price_entry(model_id, cand)
    except Exception:
        logger.debug("confidential: no usable pricing", extra={"model": model_id})
        return None
    if not entry or not all(
        is_usable_rate(entry.get(k, math.nan)) for k in ("in", "cached_in", "out")
    ):
        return None
    if "cache_write" in entry and not is_usable_rate(entry["cache_write"]):
        return None
    return entry


def priced_models(
    side_offer: dict[str, Any],
) -> dict[str, tuple[Candidate, dict[str, float]]]:
    """Offered models that have a usable rate entry: the one advertised set."""
    out: dict[str, tuple[Candidate, dict[str, float]]] = {}
    for model_id, cand in offered_models(side_offer).items():
        entry = usable_price_entry(model_id, cand)
        if entry is not None:
            out[model_id] = (cand, entry)
    return out


# ---------------------------------------------------------------------------
# Offer
# ---------------------------------------------------------------------------


def confidential_ws_public_url() -> str:
    """The public session URL; relative to the node when ``HTTP_URL`` is unset.

    Never derived from the (internal) sidecar URL.
    """
    public = (settings.http_url or "").rstrip("/")
    return f"{to_ws_url(public) if public else ''}/v1/confidential/ws"


def confidential_offer_url() -> str:
    base = (settings.http_url or "").rstrip("/")
    return f"{base}/v1/confidential/offer"


@dataclass(frozen=True)
class OfferTerms:
    """What a signed offer fixes for billing: rates, fees, surcharge and the
    reservation's completion margin."""

    price_list: dict[str, Any]
    session_fee_msat: int = 0
    rate_surcharge_pct: float = 0.0
    abandon_fee_msat: int = 0
    reserve_margin_tokens: int = 0
    # The signed ``images`` block (variants and prices), when images are offered.
    images: dict[str, Any] | None = None


# sig -> (monotonic time last served, terms), oldest first.
_SIGNED_OFFERS: OrderedDict[str, tuple[float, OfferTerms]] = OrderedDict()
_LAST_OFFER: dict[str, Any] = {"payload": None, "offer": None}


def _remember_offer(sig: str, terms: OfferTerms | dict[str, Any]) -> None:
    """Remember a signed offer's terms (a bare price list means no fee)."""
    if not isinstance(terms, OfferTerms):
        terms = OfferTerms(terms)
    now = time.monotonic()
    _SIGNED_OFFERS[sig] = (now, terms)
    _SIGNED_OFFERS.move_to_end(sig)
    while _SIGNED_OFFERS:
        oldest, (at, _) = next(iter(_SIGNED_OFFERS.items()))
        if len(_SIGNED_OFFERS) <= SIGNED_OFFER_MAX and now - at < SIGNED_OFFER_TTL_S:
            break
        del _SIGNED_OFFERS[oldest]


def sidecar_protocol_version(side: dict[str, Any]) -> int:
    """The protocol version the sidecar's offer names (1, the retired v1, if it
    names none; 0 if it is not an integer)."""
    try:
        return int(side.get("v", 1))
    except (TypeError, ValueError):
        return 0


def signed_offer_terms(sig: str) -> OfferTerms | None:
    """The terms of an offer this node signed recently."""
    found = _SIGNED_OFFERS.get(sig)
    if found is None or time.monotonic() - found[0] >= SIGNED_OFFER_TTL_S:
        return None
    return found[1]


def signed_offer_rates(sig: str, model_id: str) -> dict[str, float] | None:
    """The rates for ``model_id`` in an offer this node signed recently
    (surcharge included: they are the rates the client verified)."""
    terms = signed_offer_terms(sig)
    if terms is None:
        return None
    entry = terms.price_list.get(model_id)
    return dict(entry) if isinstance(entry, dict) else None


def signed_image_variant(
    terms: OfferTerms, model_id: str, variant_id: str
) -> dict[str, Any] | None:
    """A variant of ``model_id`` in a signed offer's images block."""
    block = terms.images or {}
    for variant in (block.get("price_list") or {}).get(model_id) or []:
        if isinstance(variant, dict) and variant.get("id") == variant_id:
            return variant
    return None


async def build_offer() -> dict[str, Any]:
    """The node's offer, signed with its Nostr key.

    The price list is a signed rate snapshot: a session whose setup names this
    offer's ``sig`` is billed at exactly these rates, and the client checks that
    the receipt never charges more than its usage at them. An unchanged offer
    is served again with the same signature.

    With a rate surcharge the price list already carries the surcharged rates
    (the ones billed), so a client's rate check needs no change; the session
    fee and the surcharge are listed beside it (only when set).
    """
    side = await cached_sidecar_offer()
    fee, abandon, pct = session_fee_msat(), abandon_fee_msat(), rate_surcharge_pct()
    prices = {
        mid: surcharged(entry, pct) for mid, (_c, entry) in priced_models(side).items()
    }
    margin = reserve_margin_tokens()
    images = images_offer_block(side, pct)
    terms = OfferTerms(prices, fee, pct, abandon, margin, images)
    offer: dict[str, Any] = {
        # Protocol v2 (the two-party handshake) only; v1 is retired.
        "v": CONFIDENTIAL_VERSION,
        "min_v": CONFIDENTIAL_MIN_V,
        "max_v": CONFIDENTIAL_MAX_V,
        "upstream_host": str(side.get("upstream_host", "")),
        "ws": confidential_ws_public_url(),
        "notary_pubkey": node_pubkey_hex() or "",
        "notary_pubkey_curve": "secp256k1-schnorr",
        "max_tokens_cap": int(side.get("max_tokens_cap", 4096)),
        "key_length": int(side.get("key_length", 0)),
        "key_alphabet": side.get("key_alphabet", "[A-Za-z0-9_-]"),
        "head_template": side.get("head_template", ""),
        "suffix_keys": side.get("suffix_keys", []),
        "price_list": prices,
        "mints": settings.cashu_mints,
        **pricing_fields(fee, abandon, pct),
    }
    chat = side.get("chat")
    if isinstance(chat, dict):
        # The pinned chat suffix (docs/CHAT-SUFFIX.md in routstr-confidential):
        # the sidecar renders it from this block and checked it at startup;
        # clients render the same bytes from the signed copy.
        offer["chat"] = chat
    if margin > 0:
        offer["reserve_margin_tokens"] = margin
    if images is not None:
        # Absent without images, so a chat-only node signs what it signed before.
        offer["capabilities"] = ["chat", "images"]
        offer["images"] = images
    payload = canonical_json(offer).decode("utf-8")
    last = _LAST_OFFER["offer"]
    if last is not None and _LAST_OFFER["payload"] == payload:
        _remember_offer(last["sig"], terms)
        return dict(last)
    sig = sign_payload(offer)
    if sig:
        offer["sig_payload"] = payload
        offer["sig"] = sig
        _remember_offer(sig, terms)
        _LAST_OFFER.update(payload=payload, offer=dict(offer))
    else:
        logger.warning("confidential: serving an UNSIGNED offer (no NSEC configured)")
    return offer


confidential_router = APIRouter()


@confidential_router.get("/v1/confidential/offer")
async def confidential_offer() -> dict[str, Any]:
    try:
        return await build_offer()
    except (OfferError, httpx.HTTPError) as exc:
        # The cause stays in the log: it can name the sidecar's address.
        logger.warning("confidential: offer unavailable: %s", exc)
        raise HTTPException(
            status_code=502, detail="confidential sidecar unavailable"
        ) from exc


async def models_fields() -> dict[str, dict[str, Any]]:
    """The additive ``confidential_upstream`` field for ``/v1/models``, by model id."""
    try:
        side = await cached_sidecar_offer()
    except Exception:
        return {}
    field = {
        "v": CONFIDENTIAL_VERSION,
        "offer_url": confidential_offer_url(),
        # Lets a client size its deposit before it fetches the offer.
        **pricing_fields(session_fee_msat(), abandon_fee_msat(), rate_surcharge_pct()),
    }
    out = {model_id: field for model_id in priced_models(side)}
    for model_id in priced_image_models(side, rate_surcharge_pct()):
        # Keyed by the provider's spelling in the offer; /v1/models lists the
        # node's lower-cased public id.
        out[model_id] = out[model_id.lower()] = {**field, "kind": "images"}
    return out


# ---------------------------------------------------------------------------
# Billing: the node's normal reservation / settlement path
# ---------------------------------------------------------------------------


@dataclass
class Billing:
    """One session's reservation against the client's ordinary API key."""

    key_hash: str
    model_id: str
    candidate: Candidate
    reserved_msats: int
    snapshot: Any  # auth.ReservationSnapshot
    # Frozen at setup: msats per 1k tokens ({"in", "cached_in", "cache_write",
    # "out"}; offers signed before cache writes were priced lack "cache_write"),
    # surcharge included.
    rates: dict[str, float]
    # Frozen at setup from the same offer; the larger fee is part of
    # ``reserved_msats``.
    session_fee_msat: int = 0
    rate_surcharge_pct: float = 0.0
    abandon_fee_msat: int = 0
    # Image sessions: the frozen variant, the count and the rendered suffix;
    # the charge is ``msat_per_image × count`` plus the session fee.
    kind: str = "chat"
    variant: str | None = None
    count: int = 1
    msat_per_image: int = 0
    suffix: str = ""
    # Chat sessions: the pinned cap and the offer's completion margin.
    max_tokens: int = 0
    reserve_margin_tokens: int = 0


class BillingError(Exception):
    """Setup refused (unknown model, insufficient balance, bad credential)."""

    def __init__(self, message: str, status: int = 400, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        # FastAPI-style detail, so clients parse it like a normal proxy error.
        self.detail = detail if detail is not None else message


def prompt_token_limit(model_obj: Any) -> int | None:
    """The largest prompt the node must reserve for: the model's context length.

    The prompt is opaque, so nothing smaller is safe. In particular the
    visible-body helper's ``context_length - max_completion_tokens`` is *not*
    a bound here: the completion cap pinned by the suffix is usually far below
    the model's own completion limit, so the provider accepts a prompt up to
    ``context_length - max_tokens`` and the difference would go unreserved
    (and, since the charge is capped at the reservation, unbilled).
    ``None`` when no context length is known (the caller then falls back to
    the model's undiscounted maximum cost). Only positive integers count: 0
    means unknown, and a negative value (bad provider metadata) would shrink
    the reservation, so ``reserve`` refuses such a model. The model's own
    context length and its catalogue's ``top_provider`` one can differ (the
    latter may describe another provider): the larger is reserved.
    """
    known = [int(cl) for cl in _context_lengths(model_obj) if _is_int(cl) and cl > 0]
    return max(known) if known else None


def _context_lengths(model_obj: Any) -> list[Any]:
    top = getattr(model_obj, "top_provider", None)
    return [
        getattr(top, "context_length", None) if top else None,
        getattr(model_obj, "context_length", None),
    ]


def _has_bad_context_length(model_obj: Any) -> bool:
    """A context length that is set but neither a non-negative integer: the
    model's derived maximum cost cannot be trusted either."""
    return any(
        cl is not None and not (_is_int(cl) and cl >= 0)
        for cl in _context_lengths(model_obj)
    )


def _undiscounted_max_cost_msats(model_obj: Any) -> int | None:
    """The model's maximum cost in msats without the tolerance discount that
    ``get_max_cost_for_model`` applies to visible-body requests."""
    sats = getattr(model_obj, "sats_pricing", None)
    max_cost = getattr(sats, "max_cost", None) if sats is not None else None
    if not isinstance(max_cost, (int, float)) or not math.isfinite(max_cost):
        return None
    if max_cost <= 0:
        return None
    return math.ceil(float(max_cost) * 1000)


def frozen_cost_msats(
    rates: dict[str, float], usage: Any, reserved_msats: int
) -> int | None:
    """The cost of the provider-reported usage at the frozen rates.

    ``None`` when the usage carries no prompt/completion counts. Prompt tokens
    are billed at ``in``, less the cached reads (at ``cached_in``, never above
    ``in``) and the cache writes (at ``cache_write``, ``in`` when the offer has
    none), both counted inside ``prompt_tokens`` as OpenAI-style usage reports
    them. That is the bound the client checks against its signed offer; the
    cost is never more than the reservation either.
    """
    from .payment.usage import parse_token_count

    if (
        not isinstance(usage, dict)
        or not {
            "prompt_tokens",
            "completion_tokens",
        }
        <= usage.keys()
    ):
        return None
    # Counts that are not plain integers bill as if absent: the reservation.
    if not all(
        isinstance(usage[k], int) and not isinstance(usage[k], bool) and usage[k] >= 0
        for k in ("prompt_tokens", "completion_tokens")
    ):
        return None
    prompt = parse_token_count(usage["prompt_tokens"])
    completion = parse_token_count(usage["completion_tokens"])
    details = usage.get("prompt_tokens_details")
    cached = parse_token_count(
        details.get("cached_tokens") if isinstance(details, dict) else 0
    )
    cached = min(max(cached, 0), prompt)
    written = min(cache_write_tokens(usage), prompt - cached)
    in_rate, out_rate = rates["in"], rates["out"]
    cached_rate = min(rates["cached_in"], in_rate)
    write_rate = rates.get("cache_write", in_rate)
    cost = math.ceil(
        (
            in_rate * (prompt - cached - written)
            + cached_rate * cached
            + write_rate * written
            + out_rate * completion
        )
        / 1000
    )
    return min(max(cost, 0), reserved_msats)


# Where OpenAI-style usage reports prompt-cache writes, first match wins:
# nested under ``prompt_tokens_details`` (Venice, OpenRouter, litellm), then
# Anthropic's top-level name (Venice repeats it there).
_CACHE_WRITE_DETAIL_KEYS = (
    "cache_creation_input_tokens",
    "cache_write_tokens",
    "cache_creation_tokens",
)


def cache_write_tokens(usage: dict[str, Any]) -> int:
    """Prompt tokens the provider reports as written to its prompt cache."""
    details = usage.get("prompt_tokens_details")
    found = (
        [details.get(k) for k in _CACHE_WRITE_DETAIL_KEYS]
        if isinstance(details, dict)
        else []
    )
    found.append(usage.get("cache_creation_input_tokens"))
    for v in found:
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return 0


def _offer_expired(model_id: str) -> BillingError:
    message = (
        f"the signed offer is unknown or expired, or does not price {model_id!r}; "
        "refetch /v1/confidential/offer"
    )
    return BillingError(
        message, 409, {"error": {"type": "offer_expired", "message": message}}
    )


def _fee_not_accepted(fee: int, abandon: int) -> BillingError:
    message = (
        f"this node charges confidential session fees (session {fee} msat, "
        f"abandoned session {abandon} msat); the setup must accept them "
        "(session_fee_msat and abandon_fee_msat from the signed offer). "
        "Update the client."
    )
    return BillingError(
        message,
        409,
        {
            "error": {
                "type": "session_fee_not_accepted",
                "message": message,
                "session_fee_msat": fee,
                "abandon_fee_msat": abandon,
            }
        },
    )


async def reserve(
    auth: str,
    model_id: str,
    max_tokens: int,
    offer_sig: str,
    accepted_fee_msat: int | None = None,
    accepted_abandon_fee_msat: int | None = None,
) -> Billing:
    """Validate the bearer, freeze the rates and reserve the worst case.

    The prompt is opaque to the node, so the reservation covers the model's
    full context length plus the pinned ``max_tokens`` (which π_C2S enforces
    before the upstream starts) and the offer's completion margin (a provider
    may bill slightly past the cap), at the frozen rates. Without a known
    context length it is the model's undiscounted maximum cost (no tolerance
    discount) plus the same margin; fixed-pricing deployments keep
    their per-request reservation. The larger of the frozen offer's session
    and abandon fees is added on top; the client must have accepted exactly
    those fees. Everything is frozen from the signed offer ``offer_sig`` names;
    an unknown or expired offer is refused (409).
    """
    from .auth import pay_for_request, validate_bearer_key
    from .core.db import create_session
    from .payment.helpers import get_max_cost_for_model

    side = await cached_sidecar_offer()
    cand = offered_models(side).get(model_id)
    if cand is None:
        raise BillingError(f"model {model_id!r} is not served confidentially")
    terms = signed_offer_terms(offer_sig)
    rates = signed_offer_rates(offer_sig, model_id)
    if terms is None or rates is None:
        raise _offer_expired(model_id)
    fee, pct = terms.session_fee_msat, terms.rate_surcharge_pct
    abandon = max(terms.abandon_fee_msat, fee)
    margin = terms.reserve_margin_tokens
    if (fee > 0 and accepted_fee_msat != fee) or (
        abandon > 0 and accepted_abandon_fee_msat != abandon
    ):
        raise _fee_not_accepted(fee, abandon)
    async with create_session() as session:
        try:
            key = await validate_bearer_key(auth, session)
        except HTTPException as exc:
            raise BillingError(str(exc.detail), exc.status_code, exc.detail) from exc
        limit = None if settings.fixed_pricing else prompt_token_limit(cand.model)
        if limit is None and not settings.fixed_pricing:
            if _has_bad_context_length(cand.model):
                raise BillingError(
                    f"model {model_id!r} has an invalid context length", 503
                )
            undiscounted = _undiscounted_max_cost_msats(cand.model)
            if undiscounted is None:
                undiscounted = await get_max_cost_for_model(
                    model_id, session, cand.model
                )
            # The full maximum (no tolerance discount: the body is opaque),
            # plus the completion margin, like the context-length branch.
            max_cost = max(
                settings.min_request_msat,
                math.ceil(
                    undiscounted * (1.0 + pct / 100.0) + margin * rates["out"] / 1000
                ),
            )
        elif limit is None:
            max_cost = await get_max_cost_for_model(model_id, session, cand.model)
            max_cost = math.ceil(max_cost * (1.0 + pct / 100.0))
        else:
            max_cost = max(
                settings.min_request_msat,
                math.ceil(
                    (
                        limit * max(rates["in"], rates.get("cache_write", 0.0))
                        + (max_tokens + margin) * rates["out"]
                    )
                    / 1000
                ),
            )
        max_cost += max(fee, abandon)
        try:
            snapshot = await pay_for_request(key, max_cost, session)
        except HTTPException as exc:
            raise BillingError(str(exc.detail), exc.status_code, exc.detail) from exc
        return Billing(
            key.hashed_key,
            model_id,
            cand,
            snapshot.reserved_msats,
            snapshot,
            rates,
            session_fee_msat=fee,
            rate_surcharge_pct=pct,
            abandon_fee_msat=abandon,
            max_tokens=max_tokens,
            reserve_margin_tokens=margin,
        )


def _warn_completion_overshoot(billing: Billing, usage: Any) -> None:
    """Log a provider that reports more completion tokens than the pinned
    cap plus the margin (the charge is still capped at the reservation)."""
    completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
    limit = billing.max_tokens + billing.reserve_margin_tokens
    if (
        billing.max_tokens > 0
        and isinstance(completion, int)
        and not isinstance(completion, bool)
        and completion > limit
    ):
        logger.warning(
            "confidential: usage exceeds the pinned max_tokens",
            extra={
                "model": billing.model_id,
                "completion_tokens": completion,
                "max_tokens": billing.max_tokens,
                "reserve_margin_tokens": billing.reserve_margin_tokens,
            },
        )


async def settle(billing: Billing, usage_event: dict[str, Any]) -> dict[str, Any]:
    """Charge the π_C3S-verified usage at the frozen rates, plus the session fee.

    Returns ``{"cost_msats", "balance_msats"}``. The cost is capped at the
    reservation (which includes the fee). A usage event without token counts
    is charged the reservation. Raises if nothing was finalized.
    """
    _warn_completion_overshoot(billing, usage_event.get("usage"))
    usage_cost = frozen_cost_msats(
        billing.rates, usage_event.get("usage"), billing.reserved_msats
    )
    if usage_cost is None:
        cost = billing.reserved_msats
    else:
        cost = min(usage_cost + billing.session_fee_msat, billing.reserved_msats)
    if cost == 0:
        return {"cost_msats": 0, "balance_msats": await release(billing)}
    return {"cost_msats": cost, "balance_msats": await _charge(billing, cost)}


async def reserve_image(
    auth: str,
    model_id: str,
    variant_id: str,
    count: int,
    offer_sig: str,
    accepted_fee_msat: int | None = None,
    accepted_abandon_fee_msat: int | None = None,
) -> Billing:
    """Validate the bearer and reserve a flat-priced image request.

    The price per image is frozen from the signed offer ``offer_sig`` names;
    the reservation is that price × ``count`` plus the larger of the frozen
    session and abandon fees, which the client must have accepted. An unknown
    or expired offer, model or variant is refused (409), like chat.
    """
    from .auth import pay_for_request, validate_bearer_key
    from .core.db import create_session

    side = await cached_sidecar_offer()
    if images_endpoint(side) is None:
        raise BillingError("this node does not serve confidential images")
    cand = candidate_for(model_id, str(side.get("upstream_host", "")))
    terms = signed_offer_terms(offer_sig)
    variant = (
        signed_image_variant(terms, model_id, variant_id) if terms is not None else None
    )
    if cand is None or terms is None or variant is None or terms.images is None:
        raise _offer_expired(model_id)
    max_count = int(terms.images.get("max_count", 1))
    if _is_int(variant.get("max_count")):
        max_count = min(max_count, variant["max_count"])
    if not _is_int(count) or not 1 <= count <= max_count:
        raise BillingError(f"count must be an integer in [1, {max_count}]")
    raw_price: Any = variant.get("msat_per_image")
    if not _is_int(raw_price) or raw_price <= 0:
        # Signed prices are whole millisats; anything else is not ours.
        raise _offer_expired(model_id)
    per_image: int = raw_price
    fee = terms.session_fee_msat
    abandon = max(terms.abandon_fee_msat, fee)
    if (fee > 0 and accepted_fee_msat != fee) or (
        abandon > 0 and accepted_abandon_fee_msat != abandon
    ):
        raise _fee_not_accepted(fee, abandon)
    suffix = render_image_suffix(
        model_id,
        str(terms.images.get("count_key", DEFAULT_IMAGE_COUNT_KEY)),
        count,
        terms.images.get("fixed_pins", []),
        variant.get("pins", []),
    )
    async with create_session() as session:
        try:
            key = await validate_bearer_key(auth, session)
        except HTTPException as exc:
            raise BillingError(str(exc.detail), exc.status_code, exc.detail) from exc
        max_cost = per_image * count + max(fee, abandon)
        try:
            snapshot = await pay_for_request(key, max_cost, session)
        except HTTPException as exc:
            raise BillingError(str(exc.detail), exc.status_code, exc.detail) from exc
        return Billing(
            key.hashed_key,
            model_id,
            cand,
            snapshot.reserved_msats,
            snapshot,
            {},
            session_fee_msat=fee,
            rate_surcharge_pct=terms.rate_surcharge_pct,
            abandon_fee_msat=abandon,
            kind="images",
            variant=variant_id,
            count=count,
            msat_per_image=per_image,
            suffix=suffix,
        )


async def settle_flat(billing: Billing) -> dict[str, Any]:
    """Charge a flat image request: the frozen price × count plus the session
    fee, capped at the reservation. Returns ``{"cost_msats", "balance_msats"}``."""
    cost = min(
        billing.msat_per_image * billing.count + billing.session_fee_msat,
        billing.reserved_msats,
    )
    if cost == 0:
        return {"cost_msats": 0, "balance_msats": await release(billing)}
    return {"cost_msats": cost, "balance_msats": await _charge(billing, cost)}


async def charge_fee(billing: Billing, fee_msat: int) -> int | None:
    """Charge exactly ``fee_msat`` (the session fee for a provider refusal, the
    abandon fee for a session that ended without a request) and release the
    rest of the reservation. Returns the key's balance."""
    return await _charge(billing, max(0, min(fee_msat, billing.reserved_msats)))


async def release(billing: Billing) -> int | None:
    """Release the whole reservation: charge nothing (the node itself failed
    or the session never reached the sidecar).

    Returns the key's balance.
    """
    from .auth import release_reservation
    from .core.db import ApiKey, create_session

    async with create_session() as session:
        if not await release_reservation(
            billing.snapshot, session, billing.reserved_msats
        ):
            raise BillingError("reservation could not be released", 500)
        key = await session.get(ApiKey, billing.key_hash)
        return int(key.balance) if key is not None else None


async def charge_reservation(billing: Billing) -> int | None:
    """The upstream received the request but the client disclosed no usage.

    Unlike EHBP, where the node reads the provider's usage itself, here the
    client controls the disclosure: charging nothing would make withholding it
    free. The full reservation is charged instead. Returns the key's balance.
    """
    return await _charge(billing, billing.reserved_msats)


async def _charge(billing: Billing, cost_msats: int) -> int | None:
    """Finalize the reservation, charging ``cost_msats`` (≤ reserved)."""
    from .core.db import ApiKey, create_session
    from .upstream.ehbp import finalize_ehbp_actual_cost_payment

    async with create_session() as session:
        key = await session.get(ApiKey, billing.key_hash)
        if key is None:
            raise BillingError("api key vanished during the session", 500)
        charged = await finalize_ehbp_actual_cost_payment(
            key,
            session,
            billing.reserved_msats,
            billing.model_id,
            {"total_msats": cost_msats},
            billing.snapshot,
        )
        if charged != cost_msats:
            raise BillingError("reservation could not be charged", 500)
        return int(key.balance)
