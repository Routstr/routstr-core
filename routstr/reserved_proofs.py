"""Operator view of reserved proofs: what may still settle, what is spent, what is stuck.

Reserved proofs are invisible to every balance the node reports. A melt that
ended ``pending`` keeps its proofs reserved under a ``melt_id`` until someone
asks the mint how the quote ended; a proof reserved without a quote is either
spent (checkstate parks those as reserved) or an outstanding token. This module
groups them so an operator can see the picture and settle what the mint will
confirm.
"""

import time
from typing import TypedDict

from cashu.core.base import MeltQuote, Proof
from cashu.wallet.crud import get_bolt11_melt_quotes, get_keysets, get_reserved_proofs
from cashu.wallet.wallet import Wallet

from .checkstate import filter_unspent_proofs
from .core.logging import get_logger
from .core.settings import settings
from .wallet import (
    _check_bolt11_payment_status_locked,
    get_wallet,
    wallet_operation_guard,
)

logger = get_logger(__name__)


class ReservedGroup(TypedDict):
    key: str
    kind: str
    mint_url: str | None
    unit: str | None
    quote_id: str | None
    local_state: str | None
    quote_amount: int | None
    fee_reserve: int | None
    created_time: int | None
    request: str | None
    proof_count: int
    proof_amount: int
    oldest_reserved_at: str | None
    hint: str


class ReservedInspection(TypedDict):
    groups: list[ReservedGroup]
    totals: dict[str, int]
    checked_at: int


class ReconcileResult(TypedDict):
    key: str
    kind: str
    mint_url: str | None
    unit: str | None
    quote_id: str | None
    mint_state: str | None
    action: str
    released_amount: int
    pruned_amount: int
    outstanding_amount: int
    error: str | None


class ReconcileOutcome(TypedDict):
    results: list[ReconcileResult]
    inspection: ReservedInspection


# Local quote state decides what a reconcile pass can do before asking the mint.
_HINTS = {
    "paid": "paid_at_mint",
    "unpaid": "releasable",
    "pending": "check_mint",
}


def _quote_state(quote: MeltQuote | None) -> str | None:
    if quote is None:
        return None
    return str(getattr(quote.state, "value", quote.state)).lower()


async def _shared_wallet() -> Wallet:
    """Any wallet handle works: proofs, keysets, and quotes share one database."""
    return await get_wallet(
        settings.primary_mint, settings.primary_mint_unit, load=False
    )


async def _load_reserved(
    wallet: Wallet,
) -> tuple[list[Proof], dict[str, tuple[str, str]], dict[str, MeltQuote]]:
    keysets = await get_keysets(db=wallet.db)
    keyset_index = {
        keyset.id: (keyset.mint_url or "", keyset.unit.name)
        for keyset in keysets
        if keyset.mint_url
    }
    reserved = await get_reserved_proofs(db=wallet.db)
    quotes = {
        quote.quote: quote for quote in await get_bolt11_melt_quotes(db=wallet.db)
    }
    return reserved, keyset_index, quotes


def _group_reserved(
    reserved: list[Proof],
    keyset_index: dict[str, tuple[str, str]],
    quotes: dict[str, MeltQuote],
) -> list[ReservedGroup]:
    groups: dict[str, ReservedGroup] = {}
    for proof in reserved:
        mint_url, unit = keyset_index.get(proof.id, (None, None))
        melt_id = getattr(proof, "melt_id", None)
        if melt_id:
            key = melt_id
            quote = quotes.get(melt_id)
            state = _quote_state(quote)
            group = groups.get(key)
            if group is None:
                group = groups[key] = ReservedGroup(
                    key=key,
                    kind="melt",
                    mint_url=mint_url or (quote.mint if quote else None),
                    unit=unit or (quote.unit if quote else None),
                    quote_id=melt_id,
                    local_state=state,
                    quote_amount=quote.amount if quote else None,
                    fee_reserve=quote.fee_reserve if quote else None,
                    created_time=quote.created_time if quote else None,
                    request=quote.request if quote else None,
                    proof_count=0,
                    proof_amount=0,
                    oldest_reserved_at=None,
                    hint=_HINTS.get(state or "", "check_mint"),
                )
        else:
            key = f"{mint_url}|{unit}|unquoted"
            group = groups.get(key)
            if group is None:
                group = groups[key] = ReservedGroup(
                    key=key,
                    kind="unquoted",
                    mint_url=mint_url,
                    unit=unit,
                    quote_id=None,
                    local_state=None,
                    quote_amount=None,
                    fee_reserve=None,
                    created_time=None,
                    request=None,
                    proof_count=0,
                    proof_amount=0,
                    oldest_reserved_at=None,
                    hint="check_mint",
                )
        group["proof_count"] += 1
        group["proof_amount"] += proof.amount
        reserved_at = getattr(proof, "time_reserved", None)
        if reserved_at and (
            group["oldest_reserved_at"] is None
            or str(reserved_at) < group["oldest_reserved_at"]
        ):
            group["oldest_reserved_at"] = str(reserved_at)
    ordered = sorted(
        groups.values(),
        key=lambda g: (g["kind"] != "melt", g["created_time"] or 0, g["key"]),
    )
    return ordered


def _totals(groups: list[ReservedGroup]) -> dict[str, int]:
    totals: dict[str, int] = {"groups": len(groups)}
    for group in groups:
        unit = group["unit"] or "unknown"
        totals[f"reserved_{unit}"] = (
            totals.get(f"reserved_{unit}", 0) + group["proof_amount"]
        )
        bucket = f"{group['hint']}_{unit}"
        totals[bucket] = totals.get(bucket, 0) + group["proof_amount"]
    return totals


async def inspect_reserved_proofs() -> ReservedInspection:
    """Group every reserved proof by melt quote without touching the mint."""
    wallet = await _shared_wallet()
    reserved, keyset_index, quotes = await _load_reserved(wallet)
    groups = _group_reserved(reserved, keyset_index, quotes)
    return ReservedInspection(
        groups=groups, totals=_totals(groups), checked_at=int(time.time())
    )


async def _prune_spent(wallet: Wallet, proofs: list[Proof]) -> tuple[int, int]:
    """Drop proofs the mint reports spent; return (pruned, still unspent) amounts."""
    unspent = await filter_unspent_proofs(proofs, wallet)
    unspent_secrets = {proof.secret for proof in unspent}
    spent = [proof for proof in proofs if proof.secret not in unspent_secrets]
    if spent:
        await wallet.invalidate(spent)
    return sum(p.amount for p in spent), sum(p.amount for p in unspent)


async def _reconcile_group(
    group: ReservedGroup, proofs: list[Proof]
) -> ReconcileResult:
    result = ReconcileResult(
        key=group["key"],
        kind=group["kind"],
        mint_url=group["mint_url"],
        unit=group["unit"],
        quote_id=group["quote_id"],
        mint_state=None,
        action="skipped",
        released_amount=0,
        pruned_amount=0,
        outstanding_amount=0,
        error=None,
    )
    mint_url, unit = group["mint_url"], group["unit"]
    if not mint_url or not unit:
        result["error"] = "keyset unknown to this wallet"
        return result
    try:
        if group["kind"] == "melt" and group["quote_id"]:
            state = await _check_bolt11_payment_status_locked(
                mint_url, unit, group["quote_id"]
            )
            result["mint_state"] = state
            if state == "unpaid":
                # get_melt_quote already released these proofs.
                result["action"] = "released"
                result["released_amount"] = group["proof_amount"]
                return result
            if state == "paid":
                # get_melt_quote invalidates only an exact amount match; ask the
                # mint about whatever is still reserved so paid dust is pruned too.
                wallet = await get_wallet(mint_url, unit)
                still_reserved = [p for p in proofs if p.reserved]
                pruned, outstanding = await _prune_spent(wallet, still_reserved)
                result["action"] = "settled_paid"
                result["pruned_amount"] = pruned + (
                    group["proof_amount"] - sum(p.amount for p in still_reserved)
                )
                result["outstanding_amount"] = outstanding
                return result
            result["action"] = "left_reserved"
            return result

        wallet = await get_wallet(mint_url, unit)
        pruned, outstanding = await _prune_spent(wallet, proofs)
        result["action"] = "checked"
        result["pruned_amount"] = pruned
        result["outstanding_amount"] = outstanding
        return result
    except Exception as e:
        logger.warning(
            "Reserved proof reconciliation failed for a group",
            extra={"key": group["key"], "mint_url": mint_url, "error": str(e)},
        )
        result["action"] = "error"
        result["error"] = str(e)
        return result


async def reconcile_reserved_proofs(key: str | None = None) -> ReconcileOutcome:
    """Ask each mint how its reserved proofs ended and settle what it confirms.

    Melt groups follow the quote: paid proofs are invalidated, unpaid ones
    released. Unquoted proofs are only pruned when the mint says spent; an
    unspent one may be a token handed to someone, so it stays reserved for a
    human to judge.
    """
    async with wallet_operation_guard():
        wallet = await _shared_wallet()
        reserved, keyset_index, quotes = await _load_reserved(wallet)
        groups = _group_reserved(reserved, keyset_index, quotes)
        by_key: dict[str, list[Proof]] = {}
        for proof in reserved:
            melt_id = getattr(proof, "melt_id", None)
            mint_url, unit = keyset_index.get(proof.id, (None, None))
            by_key.setdefault(melt_id or f"{mint_url}|{unit}|unquoted", []).append(
                proof
            )
        results = [
            await _reconcile_group(group, by_key.get(group["key"], []))
            for group in groups
            if key is None or group["key"] == key
        ]
        after = await inspect_reserved_proofs()
    return ReconcileOutcome(results=results, inspection=after)
