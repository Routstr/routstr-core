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
CONFIDENTIAL_MAX_V = 1

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

# Signed offers stay valid for setup (``offer_sig``) this long.
SIGNED_OFFER_TTL_S = 600.0
SIGNED_OFFER_MAX = 512


def to_ws_url(url: str) -> str:
    """``http``→``ws`` and ``https``→``wss``; only the scheme changes."""
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urlunsplit(parts._replace(scheme=scheme))


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
        resp = await client.get(f"{base}/offer")
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict) or "head_template" not in data:
        raise OfferError("sidecar offer is malformed")
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


def modality_allowed(model: str, text_only_ok: dict[str, Any] | None) -> bool:
    """The sidecar's text-only gate: an empty map allows all, else an allow-list.

    Input tokens are bounded by the request's byte length only for models that
    reject image/file parts, so the sidecar lists those explicitly.
    """
    return not text_only_ok or text_only_ok.get(model) is True


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
    """Models the node serves confidentially: on the sidecar host and text-only."""
    from .proxy import get_unique_models

    host = str(side_offer.get("upstream_host", ""))
    text_only_ok = side_offer.get("text_only_ok") or {}
    out: dict[str, Candidate] = {}
    for model in get_unique_models():
        if not modality_allowed(model.id, text_only_ok):
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


# sig -> (monotonic time last served, price_list), oldest first.
_SIGNED_OFFERS: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_LAST_OFFER: dict[str, Any] = {"payload": None, "offer": None}


def _remember_offer(sig: str, price_list: dict[str, Any]) -> None:
    now = time.monotonic()
    _SIGNED_OFFERS[sig] = (now, price_list)
    _SIGNED_OFFERS.move_to_end(sig)
    while _SIGNED_OFFERS:
        oldest, (at, _) = next(iter(_SIGNED_OFFERS.items()))
        if len(_SIGNED_OFFERS) <= SIGNED_OFFER_MAX and now - at < SIGNED_OFFER_TTL_S:
            break
        del _SIGNED_OFFERS[oldest]


def signed_offer_rates(sig: str, model_id: str) -> dict[str, float] | None:
    """The rates for ``model_id`` in an offer this node signed recently."""
    found = _SIGNED_OFFERS.get(sig)
    if found is None or time.monotonic() - found[0] >= SIGNED_OFFER_TTL_S:
        return None
    entry = found[1].get(model_id)
    return dict(entry) if isinstance(entry, dict) else None


async def build_offer() -> dict[str, Any]:
    """The node's offer, signed with its Nostr key.

    The price list is a signed rate snapshot: a session whose setup names this
    offer's ``sig`` is billed at exactly these rates, and the client checks that
    the receipt never charges more than its usage at them. An unchanged offer
    is served again with the same signature.
    """
    side = await cached_sidecar_offer()
    prices = {mid: entry for mid, (_c, entry) in priced_models(side).items()}
    offer: dict[str, Any] = {
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
    }
    payload = canonical_json(offer).decode("utf-8")
    last = _LAST_OFFER["offer"]
    if last is not None and _LAST_OFFER["payload"] == payload:
        _remember_offer(last["sig"], prices)
        return dict(last)
    sig = sign_payload(offer)
    if sig:
        offer["sig_payload"] = payload
        offer["sig"] = sig
        _remember_offer(sig, prices)
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
    field = {"v": CONFIDENTIAL_VERSION, "offer_url": confidential_offer_url()}
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
    # Frozen at setup: msats per 1k tokens ({"in", "cached_in", "out"}).
    rates: dict[str, float]


class BillingError(Exception):
    """Setup refused (unknown model, insufficient balance, bad credential)."""

    def __init__(self, message: str, status: int = 400, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        # FastAPI-style detail, so clients parse it like a normal proxy error.
        self.detail = detail if detail is not None else message


def prompt_token_limit(model_obj: Any) -> int | None:
    """The largest prompt the model accepts, as ``calculate_discounted_max_cost``
    derives it (``payment/helpers.py``); ``None`` when no context is known."""
    top = getattr(model_obj, "top_provider", None)
    cl = getattr(top, "context_length", None) if top else None
    mct = getattr(top, "max_completion_tokens", None) if top else None
    if cl or mct:
        if cl and mct:
            return max(0, cl - mct)
        return cl if cl else 0
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


async def reserve(
    auth: str, model_id: str, max_tokens: int, offer_sig: str | None = None
) -> Billing:
    """Validate the bearer, freeze the rates and reserve the worst case.

    The prompt is opaque to the node, so the reservation covers the model's
    full context plus the pinned ``max_tokens`` (which π_C2 enforces before the
    upstream starts), at the frozen rates. Without a known context length it
    is the model's undiscounted maximum cost; fixed-pricing deployments keep
    their per-request reservation.
    """
    from .auth import pay_for_request, validate_bearer_key
    from .core.db import create_session
    from .payment.helpers import get_max_cost_for_model

    side = await cached_sidecar_offer()
    cand = offered_models(side).get(model_id)
    if cand is None:
        raise BillingError(f"model {model_id!r} is not served confidentially")
    if offer_sig:
        rates = signed_offer_rates(offer_sig, model_id)
        if rates is None:
            raise _offer_expired(model_id)
    else:
        rates = usable_price_entry(model_id, cand)
        if rates is None:
            raise BillingError(f"model {model_id!r} has no usable pricing")
    async with create_session() as session:
        try:
            key = await validate_bearer_key(auth, session)
        except HTTPException as exc:
            raise BillingError(str(exc.detail), exc.status_code, exc.detail) from exc
        limit = None if settings.fixed_pricing else prompt_token_limit(cand.model)
        if limit is None:
            max_cost = await get_max_cost_for_model(model_id, session, cand.model)
        else:
            max_cost = max(
                settings.min_request_msat,
                math.ceil((limit * rates["in"] + max_tokens * rates["out"]) / 1000),
            )
        try:
            snapshot = await pay_for_request(key, max_cost, session)
        except HTTPException as exc:
            raise BillingError(str(exc.detail), exc.status_code, exc.detail) from exc
        return Billing(
            key.hashed_key, model_id, cand, snapshot.reserved_msats, snapshot, rates
        )


async def settle(billing: Billing, usage_event: dict[str, Any]) -> dict[str, Any]:
    """Charge the π_C3-verified usage at the frozen rates.

    Returns ``{"cost_msats", "balance_msats"}``. A usage event without token
    counts is charged the reservation. Raises if nothing was finalized.
    """
    cost = frozen_cost_msats(
        billing.rates, usage_event.get("usage"), billing.reserved_msats
    )
    if cost is None:
        cost = billing.reserved_msats
    if cost == 0:
        return {"cost_msats": 0, "balance_msats": await release(billing)}
    return {"cost_msats": cost, "balance_msats": await _charge(billing, cost)}


async def release(billing: Billing) -> int | None:
    """The upstream never ran (or answered an unbilled error): charge nothing.

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
