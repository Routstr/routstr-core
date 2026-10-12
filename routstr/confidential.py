"""Confidential-upstream mode: the node's offer and billing glue.

In this mode the client is the TLS 1.3 client of the upstream provider through
the node: a separate cu-sidecar relays the TLS records, writes the one record
that carries the provider API key and verifies the zero-knowledge proofs. The
node never sees the prompt or the response.

Billing reuses the node's normal path, like the EHBP/Tinfoil opaque-body flow:
the client authenticates with an ordinary bearer (``sk-`` key or Cashu token),
the node reserves the full-context prompt plus the pinned completion cap with
``pay_for_request``, charges the π_C3-verified usage at the rates frozen at
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

# Wire-protocol version; the offer's min_v/max_v is the only negotiation.
CONFIDENTIAL_VERSION = 1
CONFIDENTIAL_MIN_V = 1
CONFIDENTIAL_MAX_V = 2

# Upstream HTTP errors settled at zero cost once disclosed under π_C3: client
# errors, rate limiting and "overloaded" are answered before any work starts.
UNBILLED_ERROR_STATUSES = frozenset(range(400, 500)) | {503}

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


async def fetch_sidecar_offer() -> dict[str, Any]:
    base = settings.confidential_sidecar_url.rstrip("/")
    if not base:
        raise OfferError("confidential sidecar is not configured")
    async with httpx.AsyncClient(timeout=_SIDECAR_TIMEOUT) as client:
        resp = await client.get(f"{base}/offer", headers=sidecar_headers())
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict) or "head_template" not in data:
        raise OfferError("sidecar offer is malformed")
    if data.get("node_settles") is False:
        # The sidecar would sign stub receipts itself and never send the
        # ``usage`` frame this node settles on: every session would end
        # "released, no disclosure" and be charged its full reservation.
        # Refuse to offer confidential mode rather than take that money.
        logger.error(
            "confidential: the sidecar runs without --node-settles; "
            "confidential mode is disabled until it does"
        )
        raise OfferError("the confidential sidecar does not run with --node-settles")
    return data


_SIDECAR_CACHE: dict[str, Any] = {"at": 0.0, "offer": None, "failed_at": None}


async def cached_sidecar_offer() -> dict[str, Any]:
    """Cached sidecar offer: 30 s fresh, failures remembered for 10 s.

    When a refresh fails, the last good offer is served for up to 10 minutes
    (sessions still fail cleanly at the sidecar dial if it is really gone).
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
    node itself: the sidecar offer carries no prices.
    """
    allow = side_offer.get("model_allowlist")
    if allow is None:
        allow = side_offer.get("text_only_ok")
    if isinstance(allow, dict):
        return {str(k) for k, v in allow.items() if v is True}
    if isinstance(allow, list):
        return {str(k) for k in allow}
    return set()


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


def offered_models(side_offer: dict[str, Any]) -> dict[str, Candidate]:
    """Models the node serves confidentially: on the sidecar host, within the allowlist."""
    from .proxy import get_unique_models

    host = str(side_offer.get("upstream_host", ""))
    allowlist = model_allowlist(side_offer)
    out: dict[str, Candidate] = {}
    for model in get_unique_models():
        if not model_allowed(model.id, allowlist):
            continue
        cand = candidate_for(model.id, host)
        if cand is not None:
            out[model.id] = cand
    return out


def price_entry(model_id: str, cand: Candidate) -> dict[str, float] | None:
    """msats per 1k tokens, from the same rate function billing uses."""
    from .payment.cost_calculation import _get_pricing_rates

    rates = _get_pricing_rates(
        {"model": model_id}, cand.model, getattr(cand.upstream, "provider_fee", None)
    )
    if rates is None:  # fixed-pricing deployment
        in_rate = float(settings.fixed_per_1k_input_tokens) * 1000.0
        out_rate = float(settings.fixed_per_1k_output_tokens) * 1000.0
        return {"in": in_rate, "cached_in": in_rate, "out": out_rate}
    in_rate, out_rate, cache_read_rate, _cache_write = rates
    return {
        "in": float(in_rate),
        "cached_in": float(cache_read_rate),
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
    """What a signed offer fixes for billing: rates, fees, surcharge."""

    price_list: dict[str, Any]
    session_fee_msat: int = 0
    rate_surcharge_pct: float = 0.0
    abandon_fee_msat: int = 0


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
    """The protocol version the sidecar speaks (1 if it does not say)."""
    try:
        v = int(side.get("v", CONFIDENTIAL_VERSION))
    except (TypeError, ValueError):
        return CONFIDENTIAL_VERSION
    return v if CONFIDENTIAL_MIN_V <= v <= CONFIDENTIAL_MAX_V else CONFIDENTIAL_VERSION


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
    terms = OfferTerms(prices, fee, pct, abandon)
    offer: dict[str, Any] = {
        # The sidecar runs one protocol version (v2: the two-party handshake).
        "v": sidecar_protocol_version(side),
        "min_v": sidecar_protocol_version(side),
        "max_v": sidecar_protocol_version(side),
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
        raise HTTPException(
            status_code=502, detail=f"confidential sidecar unavailable: {exc}"
        ) from exc


async def models_fields() -> dict[str, dict[str, Any]]:
    """The additive ``confidential_upstream`` field for ``/v1/models``, by model id."""
    try:
        side = await cached_sidecar_offer()
    except Exception:
        return {}
    field = {
        "v": sidecar_protocol_version(side),
        "offer_url": confidential_offer_url(),
        # Lets a client size its deposit before it fetches the offer.
        **pricing_fields(session_fee_msat(), abandon_fee_msat(), rate_surcharge_pct()),
    }
    return {model_id: field for model_id in priced_models(side)}


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
    # Frozen at setup: msats per 1k tokens ({"in", "cached_in", "out"}),
    # surcharge included.
    rates: dict[str, float]
    # Frozen at setup from the same offer; the larger fee is part of
    # ``reserved_msats``.
    session_fee_msat: int = 0
    rate_surcharge_pct: float = 0.0
    abandon_fee_msat: int = 0


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
    the model's undiscounted maximum cost).
    """
    top = getattr(model_obj, "top_provider", None)
    cl = getattr(top, "context_length", None) if top else None
    if cl:
        return int(cl)
    return getattr(model_obj, "context_length", None) or None


def frozen_cost_msats(
    rates: dict[str, float], usage: Any, reserved_msats: int
) -> int | None:
    """The cost of the provider-reported usage at the frozen rates.

    ``None`` when the usage carries no prompt/completion counts. Never more
    than ``ceil((in*prompt + out*completion)/1000)``, the bound the client
    checks against its signed offer, nor more than the reservation.
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
    prompt = parse_token_count(usage["prompt_tokens"])
    completion = parse_token_count(usage["completion_tokens"])
    details = usage.get("prompt_tokens_details")
    cached = parse_token_count(
        details.get("cached_tokens") if isinstance(details, dict) else 0
    )
    cached = min(max(cached, 0), prompt)
    in_rate, out_rate = rates["in"], rates["out"]
    cached_rate = min(rates["cached_in"], in_rate)
    cost = math.ceil(
        (in_rate * (prompt - cached) + cached_rate * cached + out_rate * completion)
        / 1000
    )
    return min(max(cost, 0), reserved_msats)


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
    offer_sig: str | None = None,
    accepted_fee_msat: int | None = None,
    accepted_abandon_fee_msat: int | None = None,
) -> Billing:
    """Validate the bearer, freeze the rates and reserve the worst case.

    The prompt is opaque to the node, so the reservation covers the model's
    full context length plus the pinned ``max_tokens`` (which π_C2 enforces
    before the upstream starts), at the frozen rates. Without a known context length it
    is the model's undiscounted maximum cost; fixed-pricing deployments keep
    their per-request reservation. The larger of the frozen offer's session
    and abandon fees is added on top; the client must have accepted exactly
    those fees.
    """
    from .auth import pay_for_request, validate_bearer_key
    from .core.db import create_session
    from .payment.helpers import get_max_cost_for_model

    side = await cached_sidecar_offer()
    cand = offered_models(side).get(model_id)
    if cand is None:
        raise BillingError(f"model {model_id!r} is not served confidentially")
    if offer_sig:
        terms = signed_offer_terms(offer_sig)
        rates = signed_offer_rates(offer_sig, model_id)
        if terms is None or rates is None:
            raise _offer_expired(model_id)
        fee, pct = terms.session_fee_msat, terms.rate_surcharge_pct
        abandon = max(terms.abandon_fee_msat, fee)
    else:
        fee, abandon, pct = session_fee_msat(), abandon_fee_msat(), rate_surcharge_pct()
        base = usable_price_entry(model_id, cand)
        if base is None:
            raise BillingError(f"model {model_id!r} has no usable pricing")
        rates = surcharged(base, pct)
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
        if limit is None:
            max_cost = await get_max_cost_for_model(model_id, session, cand.model)
            max_cost = math.ceil(max_cost * (1.0 + pct / 100.0))
        else:
            max_cost = max(
                settings.min_request_msat,
                math.ceil((limit * rates["in"] + max_tokens * rates["out"]) / 1000),
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
        )


async def settle(billing: Billing, usage_event: dict[str, Any]) -> dict[str, Any]:
    """Charge the π_C3-verified usage at the frozen rates, plus the session fee.

    Returns ``{"cost_msats", "balance_msats"}``. The cost is capped at the
    reservation (which includes the fee). A usage event without token counts
    is charged the reservation. Raises if nothing was finalized.
    """
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
