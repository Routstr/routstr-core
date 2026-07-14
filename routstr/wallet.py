import asyncio
import re
import socket
import time
import typing
from typing import Any, Awaitable, Callable, TypedDict

import httpx
from cashu.core.base import MintQuote, Proof, Token
from cashu.core.mint_info import MintInfo as _CashuMintInfo
from cashu.wallet.helpers import deserialize_token_from_string
from cashu.wallet.wallet import Wallet
from pydantic_core import PydanticUndefined
from sqlmodel import col, select, update

from .core import db, get_logger
from .core.db import store_cashu_transaction
from .core.settings import settings
from .payment.lnurl import raw_send_to_lnurl

# cashu still declares Optional[X] without explicit defaults on MintInfo.
# Under pydantic v2 those are required, but real mints omit many of them.
# Default Optional fields to None at import time so balance fetches don't 422.
for _name, _field in _CashuMintInfo.model_fields.items():
    _annot = _field.annotation
    _is_optional = typing.get_origin(_annot) is typing.Union and type(
        None
    ) in typing.get_args(_annot)
    if _is_optional and _field.default is PydanticUndefined:
        _field.default = None
_CashuMintInfo.model_rebuild(force=True)

logger = get_logger(__name__)


class MintConnectionError(Exception):
    """The mint could not be reached (network transport failure).

    Maps to a 503, not a 4xx: the token is fine, the mint is just unavailable.
    """


class SourceMintConnectionError(MintConnectionError):
    """The mint that issued the incoming proofs cannot be reached."""


class TokenConsumedError(Exception):
    """A failure that happened AFTER the token's proofs were spent (melt
    succeeded, or redemption already returned) — e.g. minting on the primary
    mint or the DB credit then failed.

    Non-retryable: the same token will not work again. Seals the cause chain so
    a transport error underneath is never re-surfaced as a retryable
    mint_unreachable.
    """


# httpx base classes cover their subclasses. HTTPStatusError is excluded on
# purpose — that means the mint answered, just with an error status.
_MINT_TRANSPORT_COOLDOWN_SECONDS = 30.0
_MINT_RATE_LIMIT_BASE_COOLDOWN_SECONDS = 60.0
_MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS = 7 * 60 * 60

_TRANSPORT_EXC_TYPES: tuple[type[BaseException], ...] = (
    httpx.NetworkError,
    httpx.TimeoutException,
    ConnectionError,  # refused/reset/aborted
    socket.gaierror,  # DNS failure
    asyncio.TimeoutError,
)


class _MintRateGuard:
    """Limit concurrency and remember per-mint rate-limit cooldowns."""

    _guards: dict[str, "_MintRateGuard"] = {}

    @classmethod
    def get(cls, mint_url: str) -> "_MintRateGuard":
        concurrency = settings.mint_max_concurrency
        guard = cls._guards.get(mint_url)
        if guard is None or guard._max_concurrency != concurrency:
            guard = cls(mint_url, concurrency)
            cls._guards[mint_url] = guard
        return guard

    def __init__(self, mint_url: str, max_concurrency: int):
        self._mint_url = mint_url
        self._max_concurrency = max_concurrency
        self._semaphore = (
            asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
        )
        self._cooldown_until = 0.0
        self._cooldown_reason: str | None = None
        self._consecutive_rate_limits = 0
        self._needs_probe = False
        self._probe_lock = asyncio.Lock()

    def apply_cooldown(self, delay: float, *, reason: str | None = None) -> None:
        deadline = time.monotonic() + max(0.0, delay)
        if deadline >= self._cooldown_until:
            self._cooldown_until = deadline
            if reason is not None:
                self._cooldown_reason = reason
        elif self._cooldown_reason is None and reason is not None:
            self._cooldown_reason = reason
        self._needs_probe = True

    def apply_rate_limit_cooldown(self, retry_after: float | None = None) -> float:
        remaining = self.cooldown_remaining()
        if remaining > 0 and self._cooldown_reason == "rate_limited":
            minimum = min(
                _MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS,
                max(_MINT_RATE_LIMIT_BASE_COOLDOWN_SECONDS, retry_after or 0.0),
            )
            if minimum > remaining:
                self.apply_cooldown(minimum, reason="rate_limited")
                return minimum
            return remaining

        self._consecutive_rate_limits += 1
        base = max(_MINT_RATE_LIMIT_BASE_COOLDOWN_SECONDS, retry_after or 0.0)
        multiplier = 2 ** min(self._consecutive_rate_limits - 1, 10)
        delay = min(_MINT_RATE_LIMIT_MAX_COOLDOWN_SECONDS, base * multiplier)
        self.apply_cooldown(delay, reason="rate_limited")
        return delay

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def cooldown_reason(self) -> str | None:
        return self._cooldown_reason if self.cooldown_remaining() > 0 else None

    async def _wait_for_cooldown(self) -> None:
        while True:
            deadline = self._cooldown_until
            wait = max(0.0, deadline - time.monotonic())
            if wait <= 0:
                return
            logger.debug(
                "Mint rate guard: cooling down",
                extra={"mint_url": self._mint_url, "wait_seconds": round(wait, 2)},
            )
            await asyncio.sleep(wait)
            if self._cooldown_until <= deadline:
                return

    async def _run_probe(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        await self._wait_for_cooldown()
        logger.warning(
            "Mint cooldown ended; sending one probe request",
            extra={"event": "mint_cooldown_probe_started", "mint_url": self._mint_url},
        )
        try:
            result = await factory()
        except Exception as error:
            # Keep queued callers behind the probe. Handle rate limits here so
            # the next exponential step is recorded before another waiter can
            # acquire the probe lock.
            if _is_mint_rate_limited(error):
                retry_after = None
                if isinstance(error, httpx.HTTPStatusError):
                    retry_after = _parse_retry_after(error.response.headers)
                self.apply_rate_limit_cooldown(retry_after)
            else:
                self.apply_cooldown(1.0)
            logger.warning(
                "Mint cooldown probe failed",
                extra={
                    "event": "mint_cooldown_probe_failed",
                    "mint_url": self._mint_url,
                    "error": str(error),
                    "error_type": type(error).__name__,
                    "cooldown_seconds": round(self.cooldown_remaining(), 2),
                    "consecutive_rate_limits": self._consecutive_rate_limits,
                },
            )
            raise

        self._needs_probe = False
        self._cooldown_until = 0.0
        self._cooldown_reason = None
        self._consecutive_rate_limits = 0
        logger.warning(
            "Mint cooldown probe succeeded; restoring normal concurrency",
            extra={
                "event": "mint_cooldown_probe_succeeded",
                "mint_url": self._mint_url,
            },
        )
        return result

    async def run(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        while True:
            if self._needs_probe or self.cooldown_remaining() > 0:
                async with self._probe_lock:
                    if self.cooldown_remaining() > 0:
                        self._needs_probe = True
                    if self._needs_probe:
                        return await self._run_probe(factory)
                continue

            if self._semaphore is None:
                return await factory()
            async with self._semaphore:
                if self._needs_probe:
                    continue
                return await factory()


def _mint_cooldown_remaining(mint_url: str) -> float:
    return _MintRateGuard.get(mint_url).cooldown_remaining()


def _mint_cooldown_reason(mint_url: str) -> str | None:
    return _MintRateGuard.get(mint_url).cooldown_reason()


def _is_mint_rate_limited(error: BaseException) -> bool:
    """True if the mint returned a 429 or rate-limit indication."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            if current.response.status_code == 429:
                return True
        lowered = str(current).lower()
        if "rate limit" in lowered or "too many requests" in lowered:
            return True
        current = current.__cause__ or current.__context__
    return False


async def _mint_operation(
    factory: Callable[[], Awaitable[Any]],
    *,
    op_name: str = "mint_operation",
    mint_url: str = "",
    retry_timeouts: bool = True,
    retry_on_rate_limit: bool = True,
) -> Any:
    """Run a mint operation with bounded concurrency and adaptive cooldown.

    The timeout covers concurrency queueing, 429 cooldown, backoff, and network
    work together.  ``factory`` must return a fresh coroutine for every retry.

    When ``retry_on_rate_limit`` is False a 429 is not retried in-place — the
    cooldown is still applied to the per-mint guard (so subsequent operations on
    that mint wait), but the exception is re-raised so the caller (typically
    ``_request_mint_with_fallback``) can immediately try a different mint.
    """
    guard = _MintRateGuard.get(mint_url) if mint_url else None
    timeout = settings.mint_operation_timeout_seconds
    max_attempts = settings.mint_retry_max_attempts + 1

    async def invoke() -> Any:
        if guard is not None:
            return await guard.run(factory)
        return await factory()

    async def run_with_retries() -> Any:
        for attempt in range(max_attempts):
            try:
                return await invoke()
            except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
                if retry_timeouts and attempt < max_attempts - 1:
                    backoff = (2**attempt) + (time.monotonic() % 1.0)
                    logger.warning(
                        "Mint operation timed out, retrying",
                        extra={
                            "op_name": op_name,
                            "mint_url": mint_url,
                            "attempt": attempt + 1,
                            "backoff_seconds": round(backoff, 2),
                        },
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise httpx.TimeoutException(
                    f"{op_name} timed out (attempts: {attempt + 1})"
                ) from exc
            except Exception as exc:
                if not _is_mint_rate_limited(exc):
                    raise

                # Apply cooldown to the guard regardless — even when we're
                # about to re-raise for fallback, the guard must remember that
                # this mint is rate-limited for future operations.
                backoff = (2**attempt) + (time.monotonic() % 1.0)
                if isinstance(exc, httpx.HTTPStatusError):
                    retry_after = _parse_retry_after(exc.response.headers)
                    if retry_after is not None:
                        backoff = max(retry_after, backoff)
                cooldown = backoff
                if guard is not None:
                    cooldown = guard.apply_rate_limit_cooldown(backoff)

                # When the caller has a fallback strategy (trusted-mint
                # list), re-raise immediately so the caller can try the next
                # mint instead of waiting through this mint's cooldown.
                if not retry_on_rate_limit:
                    logger.warning(
                        "Mint rate-limited, skipping retries for fallback",
                        extra={
                            "op_name": op_name,
                            "mint_url": mint_url,
                            "cooldown_seconds": round(cooldown, 2),
                            "consecutive_rate_limits": guard._consecutive_rate_limits
                            if guard is not None
                            else attempt + 1,
                        },
                    )
                    raise

                if attempt >= max_attempts - 1:
                    raise
                logger.warning(
                    "Mint rate-limited, applying cooldown",
                    extra={
                        "op_name": op_name,
                        "mint_url": mint_url,
                        "attempt": attempt + 1,
                        "cooldown_seconds": round(cooldown, 2),
                        "consecutive_rate_limits": guard._consecutive_rate_limits
                        if guard is not None
                        else attempt + 1,
                    },
                )
                if guard is None:
                    await asyncio.sleep(cooldown)

        raise RuntimeError(f"{op_name}: exhausted retries unexpectedly")

    try:
        if timeout > 0:
            return await asyncio.wait_for(run_with_retries(), timeout=timeout)
        return await run_with_retries()
    except asyncio.TimeoutError as exc:
        raise httpx.TimeoutException(
            f"{op_name} exceeded its {timeout}s total timeout"
        ) from exc


def _parse_retry_after(headers: Any) -> float | None:
    """Parse a Retry-After header (delta-seconds form) into seconds."""
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def is_source_mint_connection_error(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SourceMintConnectionError):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_mint_connection_error(error: BaseException) -> bool:
    """True if ``error`` (or anything in its cause/context chain) is a mint
    transport failure. Walks the chain because some sites re-raise transport
    errors wrapped in ValueError/MintConnectionError; matches on TYPE, not text.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TokenConsumedError):
            # Sealed: the token was already spent, so whatever transport error
            # sits underneath must not make this look retryable.
            return False
        if isinstance(current, MintConnectionError):
            return True
        if isinstance(current, _TRANSPORT_EXC_TYPES):
            return True
        current = current.__cause__ or current.__context__
    return False


# Redemption ``code`` values whose token is spent/consumed/unusable — the
# X-Cashu path must NOT echo the original token for these (echoing invites a
# retry with a token that can never succeed again).
SPENT_TOKEN_CODES: frozenset[str] = frozenset(
    {
        "cashu_token_already_spent",
        "cashu_token_consumed",
        "cashu_token_zero_value",
        "internal_error",
    }
)


def classify_redemption_error(
    error: Exception,
) -> tuple[str, int, str, str] | None:
    """Map a token-redemption failure to ``(type, status, message, code)``.

    Single source of truth for every endpoint that redeems a token (bearer,
    X-Cashu, top-up) so the same failure yields the same taxonomy everywhere.
    ``type`` and ``code`` are stable client contract; ``message`` is sanitized
    (raw error text stays in logs). Returns None for an unclassified internal
    fault — the caller emits a generic 500.
    """
    if isinstance(error, TokenConsumedError):
        return (
            "token_consumed",
            500,
            "Token was redeemed but could not be credited; do not retry",
            "cashu_token_consumed",
        )
    if is_source_mint_connection_error(error):
        return (
            "mint_unreachable",
            503,
            "The mint that issued this Cashu token is unreachable; the token cannot be redeemed at another mint",
            "cashu_source_mint_unreachable",
        )
    if _is_mint_rate_limited(error) or is_mint_connection_error(error):
        return (
            "mint_unreachable",
            503,
            "Cashu mint is unreachable",
            "cashu_mint_unreachable",
        )
    lowered = str(error).lower()
    if "already spent" in lowered:
        return (
            "token_already_spent",
            400,
            "Cashu token already spent",
            "cashu_token_already_spent",
        )
    if (
        "insufficient" in lowered
        or "melt fee" in lowered
        or "exceed token amount" in lowered
        or "estimate fees" in lowered
    ):
        return (
            "mint_error",
            422,
            "Token value is too small to cover swap fees",
            "cashu_token_swap_fees_exceed_amount",
        )
    if "failed to melt" in lowered:
        return (
            "mint_error",
            422,
            "Failed to swap token from foreign mint",
            "cashu_foreign_mint_swap_failed",
        )
    if ("invalid" in lowered or "decode" in lowered) and "token" in lowered:
        # Anchored to "token" so internal faults whose text merely contains
        # "invalid"/"decode" fall through to the 500 branch, not a token error.
        return (
            "invalid_token",
            400,
            "Invalid Cashu token",
            "invalid_cashu_token",
        )
    if "must be positive" in lowered or "yielded no value" in lowered:
        # Redeemed to <= 0 (empty/dust token, or value fully consumed by fees).
        # Consumed, so non-retryable, but its own code — not the generic bucket.
        return (
            "cashu_error",
            400,
            "Failed to redeem Cashu token: token yielded no value",
            "cashu_token_zero_value",
        )
    if isinstance(error, ValueError):
        return (
            "cashu_error",
            400,
            "Failed to redeem Cashu token",
            "cashu_token_redemption_failed",
        )
    return None


async def get_balance(unit: str) -> int:
    wallet = await get_wallet(settings.primary_mint, unit)
    return wallet.available_balance.amount


async def _redeem_same_mint(
    wallet: Wallet, token_obj: Token
) -> tuple[int, str, str]:  # amount, unit, mint_url
    """Redeem proofs at their own issuing mint (no cross-mint swap).

    split() re-mints the incoming proofs into fresh ones we own so the sender
    can't double-spend them. With include_fees=True the mint deducts its NUT-02
    per-proof input fee, so we end up holding only `amount - input_fees`. Credit
    that, not the face value, or routstr over-credits the user and its wallet
    drifts insolvent.
    """
    try:
        await _mint_operation(
            lambda: wallet.load_mint(keyset_id=token_obj.keysets[0]),
            op_name="redeem_load_mint",
            mint_url=token_obj.mint,
        )
        wallet.verify_proofs_dleq(token_obj.proofs)
        input_fees = wallet.get_fees_for_proofs(token_obj.proofs)
        await _mint_operation(
            lambda: wallet.split(proofs=token_obj.proofs, amount=0, include_fees=True),
            op_name="redeem_split",
            mint_url=token_obj.mint,
            retry_timeouts=False,
        )
    except Exception as error:
        if is_mint_connection_error(error):
            logger.warning(
                "Same-mint redemption failed; client must use a different token",
                extra={
                    "event": "cashu_same_mint_redemption_failed",
                    "source_mint": token_obj.mint,
                    "source_unit": token_obj.unit,
                    "source_amount": token_obj.amount,
                    "cross_mint_fallback_attempted": False,
                    "action": "retry_with_token_from_another_mint",
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
            )
            raise SourceMintConnectionError(
                "Issuing Cashu mint is unreachable"
            ) from error
        raise

    return int(token_obj.amount) - input_fees, token_obj.unit, token_obj.mint


async def recieve_token(
    token: str,
) -> tuple[int, str, str]:  # amount, unit, mint_url
    token_obj = deserialize_token_from_string(token)
    if len(token_obj.keysets) > 1:
        raise ValueError("Multiple keysets per token currently not supported")

    wallet = await get_wallet(token_obj.mint, token_obj.unit, load=False)
    wallet.keyset_id = token_obj.keysets[0]

    if token_obj.mint not in settings.cashu_mints:
        destinations = list(
            dict.fromkeys([settings.primary_mint, *settings.cashu_mints])
        )
        logger.warning(
            "Cashu cross-mint swap required",
            extra={
                "event": "cashu_swap_started",
                "source_mint": token_obj.mint,
                "source_unit": token_obj.unit,
                "source_amount": token_obj.amount,
                "destination_candidates": destinations,
            },
        )
        return await swap_to_trusted_mint(token_obj, wallet)

    logger.warning(
        "Trying same-mint Cashu redemption",
        extra={
            "event": "cashu_same_mint_redemption",
            "source_mint": token_obj.mint,
            "source_unit": token_obj.unit,
            "source_amount": token_obj.amount,
            "cross_mint_fallback_on_connection_failure": False,
        },
    )
    return await _redeem_same_mint(wallet, token_obj)


async def send(amount: int, unit: str, mint_url: str | None = None) -> tuple[int, str]:
    """Create a token from the preferred mint or another funded trusted mint."""
    effective_mint_url = await find_trusted_mint_with_funds(amount, unit, mint_url)
    wallet = await get_wallet(effective_mint_url, unit)
    proofs = get_proofs_per_mint_and_unit(
        wallet, effective_mint_url, unit, not_reserved=True
    )
    proofs_for_mint = sum(proof.amount for proof in proofs)

    all_mint_urls = list({k.mint_url for k in wallet.keysets.values()})
    proof_summary = {
        f"{k.mint_url}/{k.unit.name}": sum(
            p.amount for p in wallet.proofs if p.id == k.id
        )
        for k in wallet.keysets.values()
    }
    # Show ALL proofs in DB by keyset_id, regardless of whether the loaded wallet
    # knows about that keyset. This reveals proofs orphaned under stale keysets.
    raw_proofs_by_keyset: dict[str, int] = {}
    for p in wallet.proofs:
        raw_proofs_by_keyset[p.id] = raw_proofs_by_keyset.get(p.id, 0) + p.amount
    logger.info(
        f"send: proof inventory | mint={effective_mint_url} unit={unit} amount={amount} "
        f"primary_mint={settings.primary_mint} proofs_for_mint={proofs_for_mint} "
        f"all_mints={all_mint_urls} by_keyset={proof_summary} "
        f"raw_proofs_by_keyset_id={raw_proofs_by_keyset} "
        f"total_wallet_proofs={sum(p.amount for p in wallet.proofs)}"
    )

    # Reserve proofs only after serialization succeeds — if serialize_proofs or
    # swap_to_send fails mid-way, proofs stay unreserved so dashboard balance
    # doesn't go negative.
    send_proofs, _ = await wallet.select_to_send(
        proofs, amount, set_reserved=False, include_fees=False
    )
    try:
        token = await wallet.serialize_proofs(
            send_proofs, include_dleq=False, legacy=False, memo=None
        )
    except Exception:
        await wallet.set_reserved_for_send(send_proofs, reserved=False)
        raise
    await wallet.set_reserved_for_send(send_proofs, reserved=True)
    return amount, token


async def send_token(amount: int, unit: str, mint_url: str | None = None) -> str:
    _, token = await send(amount, unit, mint_url)
    return token


async def release_token_reservation(token: str) -> None:
    """Release a token that was created locally but never handed off."""
    token_obj = deserialize_token_from_string(token)
    wallet = await get_wallet(token_obj.mint, token_obj.unit, load=False)
    await wallet.set_reserved_for_send(token_obj.proofs, reserved=False)

    secrets = {proof.secret for proof in token_obj.proofs}
    for proof in token_obj.proofs:
        proof.reserved = False
    for proof in wallet.proofs:
        if proof.secret in secrets:
            proof.reserved = False


def token_mint_url(token: str, fallback: str | None = None) -> str:
    try:
        return str(deserialize_token_from_string(token).mint)
    except Exception:
        if fallback is None:
            raise
        return fallback


async def find_trusted_mint_with_funds(
    amount: int, unit: str, preferred_mint: str | None = None
) -> str:
    """Choose a trusted mint that can cover a refund without waiting on cooldown."""
    trusted = list(dict.fromkeys([settings.primary_mint, *settings.cashu_mints]))
    candidates: list[str] = []
    if preferred_mint in trusted:
        candidates.append(preferred_mint)
    candidates.extend(mint for mint in trusted if mint not in candidates)

    balances: dict[str, int] = {}
    for mint_url in candidates:
        if _mint_cooldown_remaining(mint_url) > 0:
            continue
        try:
            wallet = await get_wallet(mint_url, unit, retry_on_rate_limit=False)
        except Exception as error:
            if is_mint_connection_error(error) or _is_mint_rate_limited(error):
                balances[mint_url] = 0
                continue
            raise

        proofs = get_proofs_per_mint_and_unit(wallet, mint_url, unit, not_reserved=True)
        balances[mint_url] = sum(proof.amount for proof in proofs)
        if balances[mint_url] >= amount:
            return mint_url

    raise ValueError(
        f"No trusted mint has {amount} {unit} available; balances={balances}"
    )


# A foreign mint's fee_reserve is a non-binding estimate (NUT-05): the mint may
# demand more when re-quoting or at melt execution. Instead of padding the
# estimate with a safety buffer (which strands the margin at the foreign mint
# on every swap), the swap retries with the amount recomputed from the fees the
# mint actually demands, up to this many attempts.
_MAX_SWAP_ATTEMPTS = 3

_MINT_ERROR_CODE_RE = re.compile(r"\(Code: (\d+)\)")
_MELT_SHORTFALL_RE = re.compile(r"Provided: (\d+), needed: (\d+)")

# Insufficient-melt-inputs failures differ across mint implementations. 11005 is
# the registered "Transaction is not balanced" code (cdk), specific enough to
# trust on the code alone. 11000 is nutshell's generic, unregistered
# TransactionError covering many unrelated failures, so it only counts as a fee
# shortfall alongside the "not enough inputs" detail text. With no code suffix at
# all, that same text is the only signal.


def _net_minted_amount(amount_msat: int, token_unit: str, fees: int) -> int:
    """
    Convert the token value minus fees (given in the token unit) into an
    amount in the primary mint's unit.
    """
    fee_msat = fees * 1000 if token_unit == "sat" else fees
    remaining_msat = amount_msat - fee_msat
    if settings.primary_mint_unit == "sat":
        return int(remaining_msat // 1000)
    return int(remaining_msat)


def _melt_insufficient_shortfall(error: Exception) -> int | None:
    """
    Classify a melt failure: return the observed shortfall (in the token unit)
    when the mint rejected the inputs as insufficient, or None when the failure
    is unrelated to fees and must not be retried (e.g. a Lightning payment
    failure, where a smaller invoice would not help).

    Cashu errors carry no structured amounts (NUT-00 defines only detail/code,
    flattened to "Mint Error: <detail> (Code: <code>)" by cashu-py), so the
    classification uses the code and the shortfall must be inferred: the
    "Provided: X, needed: Y" amounts are nutshell-specific free text and only
    refine the shortfall when present; otherwise shrink one unit at a time.
    """
    message = str(error)
    code_match = _MINT_ERROR_CODE_RE.search(message)
    code = code_match.group(1) if code_match is not None else None
    has_shortfall_text = "not enough inputs" in message.lower()

    match code:
        case "11005":  # registered TransactionUnbalanced: trust the code
            pass
        case "11000" if has_shortfall_text:  # generic nutshell error: needs the text
            pass
        case None if has_shortfall_text:  # no code suffix: text is the only signal
            pass
        case _:  # other codes, a bare 11000, or no signal: must not retry
            return None

    amounts = _MELT_SHORTFALL_RE.search(message)
    if amounts is not None:
        provided, needed = int(amounts.group(1)), int(amounts.group(2))
        if needed > provided:
            return needed - provided
    return 1


async def _request_mint_with_fallback(
    amount: int, *, op_name: str, primary_wallet: Wallet | None = None
) -> tuple[Wallet, str, MintQuote]:
    """Try request_mint on the primary mint, fall back to other trusted mints
    on transport or rate-limit failure. Returns the wallet, mint_url, and quote.

    Guards against amount <= 0: the cashu library's PostMintQuoteRequest
    enforces ``amount > 0`` (Pydantic Field(gt=0)), so passing 0 raises a
    cryptic validation error deep in the stack.  Fail fast with context.
    """
    if amount <= 0:
        raise ValueError(
            f"_request_mint_with_fallback({op_name}): amount must be > 0, got {amount}. "
            f"Token value is too small after fee deduction or unit conversion."
        )
    candidates = list(dict.fromkeys([settings.primary_mint, *settings.cashu_mints]))
    logger.warning(
        "Trying trusted destination mints",
        extra={
            "event": "cashu_destination_candidates",
            "op_name": op_name,
            "amount": amount,
            "unit": settings.primary_mint_unit,
            "candidates": candidates,
        },
    )
    tried: list[str] = []
    for candidate_index, mint_url in enumerate(candidates, start=1):
        cooldown = _mint_cooldown_remaining(mint_url)
        if cooldown > 0:
            tried.append(f"{mint_url}: cooling down")
            logger.warning(
                "Skipping unavailable destination mint",
                extra={
                    "event": "cashu_destination_skipped",
                    "mint_url": mint_url,
                    "cooldown_seconds": round(cooldown, 2),
                    "op_name": op_name,
                    "candidate_index": candidate_index,
                    "candidate_count": len(candidates),
                },
            )
            continue
        logger.warning(
            "Trying destination mint",
            extra={
                "event": "cashu_destination_attempt",
                "mint_url": mint_url,
                "op_name": op_name,
                "candidate_index": candidate_index,
                "candidate_count": len(candidates),
            },
        )
        try:
            if mint_url == settings.primary_mint and primary_wallet is not None:
                wallet = primary_wallet
            else:
                wallet = await get_wallet(
                    mint_url,
                    settings.primary_mint_unit,
                    retry_on_rate_limit=False,
                )
            quote = await _mint_operation(
                lambda: wallet.request_mint(amount),
                op_name=op_name,
                mint_url=mint_url,
                retry_on_rate_limit=False,
            )
            logger.warning(
                "Destination mint selected",
                extra={
                    "event": "cashu_destination_selected",
                    "mint_url": mint_url,
                    "op_name": op_name,
                    "candidate_index": candidate_index,
                    "fallback_used": candidate_index > 1,
                },
            )
            return wallet, mint_url, quote
        except Exception as error:
            tried.append(f"{mint_url}: {type(error).__name__}")
            connection_failure = is_mint_connection_error(error)
            rate_limited = _is_mint_rate_limited(error)
            if not connection_failure and not rate_limited:
                raise
            if connection_failure:
                _MintRateGuard.get(mint_url).apply_cooldown(
                    _MINT_TRANSPORT_COOLDOWN_SECONDS, reason="unreachable"
                )
            logger.warning(
                "Destination mint failed",
                extra={
                    "event": "cashu_destination_failed",
                    "failed_mint": mint_url,
                    "error": str(error),
                    "error_type": type(error).__name__,
                    "connection_failure": connection_failure,
                    "rate_limited": rate_limited,
                    "tried": tried,
                    "op_name": op_name,
                    "candidate_index": candidate_index,
                    "candidate_count": len(candidates),
                },
            )
            continue
    logger.error(
        "All trusted destination mints failed",
        extra={
            "event": "cashu_destination_exhausted",
            "op_name": op_name,
            "amount": amount,
            "unit": settings.primary_mint_unit,
            "candidates": candidates,
            "tried": tried,
        },
    )
    raise MintConnectionError(f"All mints failed for {op_name}: {tried}")


async def _calculate_swap_amount(
    amount_msat: int,
    token_unit: str,
    token_mint_url: str,
    token_wallet: Wallet,
    primary_wallet: Wallet | None,
    proofs: list,
) -> int:
    """
    Calculate the amount to mint on the primary mint after accounting for
    melt fees and NUT-02 input fees on the foreign mint.
    """
    if settings.primary_mint_unit == "sat":
        receive_amount = amount_msat // 1000
    else:
        receive_amount = amount_msat

    if token_mint_url == settings.primary_mint:
        logger.info(
            "swap_to_primary_mint: skipping fee estimation (same mint)",
            extra={"minted_amount": receive_amount},
        )
        return int(receive_amount)

    # The cashu library's PostMintQuoteRequest enforces amount > 0 (Pydantic
    # Field(gt=0)).  When the token's face value in the primary mint's unit
    # truncates to 0 (e.g. < 1000 msat with a "sat" primary unit), calling
    # request_mint(0) raises a validation error that is cryptic in production
    # logs.  Guard early with full diagnostic context instead.
    if receive_amount <= 0:
        logger.error(
            "swap_to_primary_mint: receive_amount is zero or negative, cannot estimate fees",
            extra={
                "amount_msat": amount_msat,
                "token_unit": token_unit,
                "token_mint_url": token_mint_url,
                "primary_mint": settings.primary_mint,
                "primary_mint_unit": settings.primary_mint_unit,
                "receive_amount": receive_amount,
            },
        )
        raise ValueError(
            f"Token amount ({amount_msat} msat, unit={token_unit}) is too small to "
            f"swap to primary mint ({settings.primary_mint}, unit={settings.primary_mint_unit}): "
            f"receive_amount={receive_amount}. Minimum 1 {settings.primary_mint_unit} required."
        )

    logger.info(
        "swap_to_primary_mint: estimating fees",
        extra={
            "dummy_amount": receive_amount,
            "unit": settings.primary_mint_unit,
            "token_mint_url": token_mint_url,
            "primary_mint": settings.primary_mint,
            "amount_msat": amount_msat,
        },
    )

    stage = "destination_fee_quote"
    try:
        _, _, dummy_mint_quote = await _request_mint_with_fallback(
            receive_amount,
            op_name="swap_fee_est_mint_quote",
            primary_wallet=primary_wallet,
        )
        stage = "source_fee_quote"
        dummy_melt_quote = await _mint_operation(
            lambda: token_wallet.melt_quote(dummy_mint_quote.request),
            op_name="swap_fee_est_melt_quote",
            mint_url=token_mint_url,
        )

        fee_reserve = dummy_melt_quote.fee_reserve
        input_fees = token_wallet.get_fees_for_proofs(proofs)
        total_fees = fee_reserve + input_fees
        minted_amount = _net_minted_amount(amount_msat, token_unit, total_fees)

        if minted_amount <= 0:
            raise ValueError(f"Fees ({total_fees} {token_unit}) exceed token amount")

        logger.info(
            "swap_to_primary_mint: fee estimation result",
            extra={
                "token_amount_sat": amount_msat // 1000,
                "estimated_fee": total_fees,
                "estimated_fee_unit": token_unit,
                "input_fees": input_fees,
                "minted_amount": minted_amount,
                "minted_unit": settings.primary_mint_unit,
                "fee_reserve": fee_reserve,
                "token_mint_url": token_mint_url,
                "primary_mint": settings.primary_mint,
            },
        )
        return minted_amount

    except Exception as e:
        logger.error(
            "Cashu swap fee estimation failed",
            extra={
                "event": "cashu_swap_fee_estimation_failed",
                "stage": stage,
                "error": str(e),
                "error_type": type(e).__name__,
                "amount_msat": amount_msat,
                "token_unit": token_unit,
                "token_mint_url": token_mint_url,
                "primary_mint": settings.primary_mint,
                "primary_mint_unit": settings.primary_mint_unit,
                "receive_amount": receive_amount,
            },
        )
        if is_mint_connection_error(e):
            if stage == "source_fee_quote":
                logger.error(
                    "Source mint is unreachable; destination fallback cannot spend its proofs",
                    extra={
                        "event": "cashu_source_mint_unreachable",
                        "source_mint": token_mint_url,
                        "stage": stage,
                        "fallback_possible": False,
                        "reason": "cashu_proofs_are_bound_to_the_issuing_mint",
                    },
                )
                raise SourceMintConnectionError(
                    "Issuing Cashu mint is unreachable"
                ) from e
            raise MintConnectionError("Cashu mint is unreachable") from e
        raise ValueError(f"Failed to estimate fees: {e}") from e


async def swap_to_trusted_mint(
    token_obj: Token, token_wallet: Wallet
) -> tuple[int, str, str]:
    logger.warning(
        "Starting Cashu cross-mint swap",
        extra={
            "event": "cashu_swap_started",
            "source_mint": token_obj.mint,
            "token_amount": token_obj.amount,
            "unit": token_obj.unit,
            "primary_mint": settings.primary_mint,
        },
    )
    # Ensure amount is an integer
    if not isinstance(token_obj.amount, int):
        token_amount = int(token_obj.amount)
    else:
        token_amount = token_obj.amount

    if token_obj.unit == "sat":
        amount_msat = token_amount * 1000
    elif token_obj.unit == "msat":
        amount_msat = token_amount
    else:
        raise ValueError("Invalid unit")
    # If the token is already from the primary mint, we don't need a cross-mint
    # swap — redeem it same-mint. There's no melt/Lightning fee, but the mint's
    # NUT-02 input fee still applies; _redeem_same_mint accounts for it.
    if token_obj.mint == settings.primary_mint:
        logger.info(
            "swap_to_primary_mint: token already on primary mint, skipping swap",
            extra={
                "mint": token_obj.mint,
                "amount": token_amount,
                "unit": token_obj.unit,
            },
        )
        return await _redeem_same_mint(token_wallet, token_obj)

    primary_wallet: Wallet | None = None

    minted_amount = await _calculate_swap_amount(
        amount_msat,
        token_obj.unit,
        token_obj.mint,
        token_wallet,
        primary_wallet,
        token_obj.proofs,
    )

    # The estimate above is non-binding: the mint may demand a higher fee on the
    # real quote or reject the melt outright. Retry the quote/melt cycle with the
    # amount recomputed from the fees the mint actually demands.
    observed_extra_fee = 0
    attempt = 0
    dest_wallet = primary_wallet
    dest_mint_url = settings.primary_mint
    while True:
        attempt += 1
        if minted_amount <= 0:
            logger.error(
                "swap_to_primary_mint: minted_amount is zero or negative before requesting quote",
                extra={
                    "minted_amount": minted_amount,
                    "attempt": attempt,
                    "foreign_mint": token_obj.mint,
                    "token_amount": token_amount,
                    "token_unit": token_obj.unit,
                    "amount_msat": amount_msat,
                    "observed_extra_fee": observed_extra_fee,
                    "primary_mint": settings.primary_mint,
                },
            )
            raise ValueError(
                f"Cannot swap token ({token_amount} {token_obj.unit}) from {token_obj.mint}: "
                f"minted_amount={minted_amount} after fee deduction (attempt {attempt})"
            )
        dest_wallet, dest_mint_url, mint_quote = await _request_mint_with_fallback(
            minted_amount,
            op_name="swap_request_mint",
            primary_wallet=primary_wallet,
        )
        logger.info(
            "swap_to_primary_mint: mint quote received",
            extra={
                "mint_quote_id": mint_quote.quote,
                "attempt": attempt,
                "dest_mint": dest_mint_url,
            },
        )

        logger.warning(
            "Requesting melt quote from source mint",
            extra={
                "event": "cashu_source_melt_quote_attempt",
                "source_mint": token_obj.mint,
                "destination_mint": dest_mint_url,
                "attempt": attempt,
            },
        )
        try:
            melt_quote = await _mint_operation(
                lambda: token_wallet.melt_quote(mint_quote.request),
                op_name="swap_melt_quote",
                mint_url=token_obj.mint,
            )
        except Exception as error:
            if is_mint_connection_error(error):
                logger.error(
                    "Source mint is unreachable; destination fallback cannot spend its proofs",
                    extra={
                        "event": "cashu_source_mint_unreachable",
                        "source_mint": token_obj.mint,
                        "destination_mint": dest_mint_url,
                        "stage": "source_melt_quote",
                        "error": str(error),
                        "error_type": type(error).__name__,
                        "attempt": attempt,
                    },
                )
                raise SourceMintConnectionError(
                    "Issuing Cashu mint is unreachable"
                ) from error
            raise
        input_fees = token_wallet.get_fees_for_proofs(token_obj.proofs)
        total_needed = melt_quote.amount + melt_quote.fee_reserve + input_fees
        logger.info(
            "swap_to_primary_mint: melt quote received",
            extra={
                "melt_quote_id": melt_quote.quote,
                "melt_amount": melt_quote.amount,
                "melt_fee_reserve": melt_quote.fee_reserve,
                "input_fees": input_fees,
                "total_needed": total_needed,
                "token_amount": token_amount,
                "attempt": attempt,
            },
        )

        if total_needed > token_amount:
            recomputed = _net_minted_amount(
                amount_msat,
                token_obj.unit,
                melt_quote.fee_reserve + input_fees + observed_extra_fee,
            )
            if attempt >= _MAX_SWAP_ATTEMPTS or recomputed <= 0:
                logger.warning(
                    "swap_to_primary_mint: insufficient token amount for melt fees",
                    extra={
                        "token_amount": token_amount,
                        "melt_amount": melt_quote.amount,
                        "melt_fee_reserve": melt_quote.fee_reserve,
                        "input_fees": input_fees,
                        "total_needed": total_needed,
                        "shortfall": total_needed - token_amount,
                        "attempts": attempt,
                    },
                )
                raise ValueError(
                    f"Token amount ({token_amount} {token_obj.unit}) is insufficient to cover "
                    f"melt fees. Needed: {total_needed} {token_obj.unit} "
                    f"(amount: {melt_quote.amount} + fee: {melt_quote.fee_reserve} + input_fees: {input_fees})"
                )
            logger.warning(
                "swap_to_primary_mint: melt quote exceeds token amount, retrying",
                extra={
                    "total_needed": total_needed,
                    "token_amount": token_amount,
                    "retry_minted_amount": recomputed,
                    "attempt": attempt,
                },
            )
            minted_amount = recomputed
            continue

        try:
            _ = await _mint_operation(
                lambda: token_wallet.melt(
                    proofs=token_obj.proofs,
                    invoice=mint_quote.request,
                    fee_reserve_sat=melt_quote.fee_reserve,
                    quote_id=melt_quote.quote,
                ),
                op_name="swap_melt",
                mint_url=token_obj.mint,
                retry_timeouts=False,
            )
        except Exception as e:
            # A down mint won't fix itself by retrying with a smaller amount.
            if is_mint_connection_error(e):
                logger.error(
                    "Source mint became unreachable during melt",
                    extra={
                        "event": "cashu_source_mint_unreachable",
                        "stage": "source_melt",
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "source_mint": token_obj.mint,
                        "destination_mint": dest_mint_url,
                        "attempt": attempt,
                    },
                )
                raise SourceMintConnectionError(
                    "Issuing Cashu mint is unreachable"
                ) from e
            shortfall = _melt_insufficient_shortfall(e)
            recomputed = 0
            if shortfall is not None:
                observed_extra_fee += shortfall
                recomputed = _net_minted_amount(
                    amount_msat,
                    token_obj.unit,
                    melt_quote.fee_reserve + input_fees + observed_extra_fee,
                )
            if shortfall is None or attempt >= _MAX_SWAP_ATTEMPTS or recomputed <= 0:
                logger.error(
                    "swap_to_primary_mint: melt failed",
                    extra={
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "foreign_mint": token_obj.mint,
                        "token_amount": token_amount,
                        "melt_quote_id": melt_quote.quote,
                        "total_needed": total_needed,
                        "attempts": attempt,
                    },
                )
                raise ValueError(
                    f"Failed to melt token from foreign mint {token_obj.mint}: {e}"
                ) from e
            logger.warning(
                "swap_to_primary_mint: mint demanded more than quoted at melt, retrying",
                extra={
                    "shortfall": shortfall,
                    "retry_minted_amount": recomputed,
                    "attempt": attempt,
                },
            )
            minted_amount = recomputed
            continue

        break

    logger.warning(
        "Source melt succeeded; minting on destination",
        extra={
            "event": "cashu_destination_mint_attempt",
            "minted_amount": minted_amount,
            "mint_quote_id": mint_quote.quote,
            "dest_mint": dest_mint_url,
        },
    )

    await dest_wallet.load_proofs(reload=True)
    pre_mint_balance = dest_wallet.available_balance.amount
    try:
        _ = await _mint_operation(
            lambda: dest_wallet.mint(minted_amount, quote_id=mint_quote.quote),
            op_name="swap_mint_on_primary",
            mint_url=dest_mint_url,
            retry_timeouts=False,
        )
    except Exception as e:
        if "11003" in str(e) or "outputs already signed" in str(e).lower():
            # Previous mint call signed outputs at the mint but failed before
            # bump_secret_derivation ran locally. Recover orphaned proofs and
            # advance the counter so the next request derives fresh secrets.
            logger.warning(
                "swap_to_primary_mint: outputs already signed — recovering orphaned proofs",
                extra={
                    "mint_quote_id": mint_quote.quote,
                    "minted_amount": minted_amount,
                },
            )
            try:
                for keyset_id in dest_wallet.keysets:
                    await dest_wallet.restore_tokens_for_keyset(
                        keyset_id, to=1, batch=25
                    )
                await dest_wallet.load_proofs(reload=True)
                post_recovery_balance = dest_wallet.available_balance.amount
                balance_gained = post_recovery_balance - pre_mint_balance
                logger.info(
                    "swap_to_primary_mint: recovery scan completed",
                    extra={
                        "pre_mint_balance": pre_mint_balance,
                        "post_recovery_balance": post_recovery_balance,
                        "balance_gained": balance_gained,
                        "expected": minted_amount,
                    },
                )
                if balance_gained < minted_amount:
                    # Recovery scan ran but did NOT restore the orphaned proofs
                    # (mint reports them as spent — they're stuck). Refuse to
                    # credit the API key balance for proofs we don't actually hold.
                    raise TokenConsumedError(
                        f"Swap recovery failed: mint signed outputs but proofs are "
                        f"unrecoverable (mint reports them spent). "
                        f"Expected {minted_amount}, recovered {balance_gained}. "
                        f"Local wallet DB ('.wallet/') state is corrupted — "
                        f"the counter for keyset is stuck at a bad index range."
                    )
            except TokenConsumedError:
                raise
            except Exception as recovery_err:
                logger.error(
                    "swap_to_primary_mint: recovery failed",
                    extra={"error": str(recovery_err)},
                )
                raise TokenConsumedError(
                    f"Mint on primary failed and recovery unsuccessful: {e}"
                ) from e
        else:
            logger.error(
                "swap_to_primary_mint: mint on primary failed after successful melt",
                extra={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "minted_amount": minted_amount,
                    "mint_quote_id": mint_quote.quote,
                },
            )
            # Foreign proofs already melted (spent) — non-retryable.
            raise TokenConsumedError(
                "Mint on primary failed after successful melt"
            ) from e

    logger.warning(
        "Cashu cross-mint swap completed",
        extra={
            "event": "cashu_swap_completed",
            "source_mint": token_obj.mint,
            "dest_mint": dest_mint_url,
            "original_amount": token_amount,
            "minted_amount": minted_amount,
            "unit": settings.primary_mint_unit,
        },
    )

    return int(minted_amount), settings.primary_mint_unit, dest_mint_url


async def swap_to_primary_mint(
    token_obj: Token, token_wallet: Wallet
) -> tuple[int, str, str]:
    """Backward-compatible alias for callers using the old function name."""
    return await swap_to_trusted_mint(token_obj, token_wallet)


async def credit_balance(
    cashu_token: str, key: db.ApiKey, session: db.AsyncSession
) -> int:
    logger.info(
        "Starting Cashu balance credit",
        extra={
            "event": "cashu_credit_started",
            "key_hash": key.hashed_key[:8],
        },
    )

    try:
        amount, unit, mint_url = await recieve_token(cashu_token)
        original_amount = amount
        original_unit = unit
        logger.info(
            "credit_balance: Token redeemed successfully",
            extra={"amount": amount, "unit": unit, "mint_url": mint_url},
        )

        if unit == "sat":
            amount = amount * 1000
            logger.info(
                "credit_balance: Converted to msat", extra={"amount_msat": amount}
            )

        # Guard against zero/negative redemptions (empty or dust tokens, or
        # swap-to-primary-mint amounts that net to <= 0 after fees). Raising here
        # — before the UPDATE/commit below — leaves any freshly-created, still
        # uncommitted ApiKey row to be rolled back when the request session
        # closes, instead of persisting an orphan key with balance 0.
        if amount <= 0:
            logger.error(
                "credit_balance: Redeemed amount is zero or negative; refusing to credit",
                extra={"amount": amount, "unit": unit, "mint_url": mint_url},
            )
            raise ValueError(
                f"Redeemed token amount must be positive, got {amount} msats"
            )

        logger.info(
            "credit_balance: Updating balance",
            extra={"old_balance": key.balance, "credit_amount": amount},
        )

        # The token is already redeemed (spent) here, so any crediting failure
        # is post-redemption and non-retryable — surface it as TokenConsumedError
        # (a key that vanished mid-flight, or an unexpected DB fault), never a
        # retryable/token-error taxonomy.
        try:
            # Atomic UPDATE to prevent race conditions during concurrent topups.
            stmt = (
                update(db.ApiKey)
                .where(col(db.ApiKey.hashed_key) == key.hashed_key)
                .values(balance=(db.ApiKey.balance) + amount)
            )
            result = await session.exec(stmt)  # type: ignore[call-overload]
            # If pruning removed this key after redemption, do not commit a no-op
            # balance update and pretend the top-up succeeded.
            if (getattr(result, "rowcount", 0) or 0) == 0:
                raise TokenConsumedError(
                    "Token redeemed but the API key disappeared before the "
                    "credit could be recorded"
                )
            await session.commit()
            await session.refresh(key)
        except TokenConsumedError:
            raise
        except Exception as db_error:
            raise TokenConsumedError(
                "Token redeemed but crediting the balance failed"
            ) from db_error

        logger.info(
            "credit_balance: Balance updated successfully",
            extra={"new_balance": key.balance},
        )

        try:
            await store_cashu_transaction(
                token=cashu_token,
                amount=original_amount,
                unit=original_unit,
                mint_url=mint_url,
                typ="in",
                source="apikey",
                api_key_hashed_key=key.hashed_key,
            )
        except Exception:
            pass

        logger.debug(
            "Cashu token successfully redeemed and stored",
            extra={"amount": amount, "unit": unit, "mint_url": mint_url},
        )
        return amount
    except Exception as e:
        logger.error(
            "credit_balance: Error during token redemption",
            extra={"error": str(e), "error_type": type(e).__name__},
        )
        raise


_wallets: dict[str, Wallet] = {}
_wallet_last_load: dict[str, float] = {}
_wallet_load_locks: dict[str, asyncio.Lock] = {}
# Minimum seconds between full mint info + proof reloads for the same
# wallet. Prevents redundant mint API calls when get_wallet(load=True)
# is called rapidly by multiple background tasks (balance fetch, payout,
# auto-topup all hitting get_wallet within the same cycle).
_WALLOAD_RELOAD_MIN_INTERVAL_SECONDS = 30


async def get_wallet(
    mint_url: str,
    unit: str = "sat",
    load: bool = True,
    retry_on_rate_limit: bool = True,
) -> Wallet:
    global _wallets, _wallet_last_load, _wallet_load_locks
    id = f"{mint_url}_{unit}"
    lock = _wallet_load_locks.setdefault(id, asyncio.Lock())
    async with lock:
        if id not in _wallets:
            _wallets[id] = await Wallet.with_db(mint_url, db=".wallet", unit=unit)

        if load:
            now = time.monotonic()
            last = _wallet_last_load.get(id)
            if last is None or now - last >= _WALLOAD_RELOAD_MIN_INTERVAL_SECONDS:
                await _mint_operation(
                    lambda: _wallets[id].load_mint(),
                    op_name="load_mint",
                    mint_url=mint_url,
                    retry_on_rate_limit=retry_on_rate_limit,
                )
                await _mint_operation(
                    lambda: _wallets[id].load_proofs(reload=True),
                    op_name="load_proofs",
                    mint_url=mint_url,
                    retry_on_rate_limit=retry_on_rate_limit,
                )
                _wallet_last_load[id] = time.monotonic()
        return _wallets[id]


def get_proofs_per_mint_and_unit(
    wallet: Wallet, mint_url: str, unit: str, not_reserved: bool = False
) -> list[Proof]:
    valid_keyset_ids = [
        k.id
        for k in wallet.keysets.values()
        if k.mint_url == mint_url and k.unit.name == unit
    ]
    proofs = [p for p in wallet.proofs if p.id in valid_keyset_ids]
    if not_reserved:
        proofs = [p for p in proofs if not p.reserved]
    return proofs


async def slow_filter_spend_proofs(
    proofs: list[Proof],
    wallet: Wallet,
    *,
    retry_on_rate_limit: bool = True,
) -> list[Proof]:
    if not proofs:
        return []
    _proofs = []
    _spent_proofs = []
    # Keep proof-state checks in large batches. Mint quotas count HTTP requests,
    # so smaller batches make balance reads slower and more likely to hit 429s.
    batch_size = 1000
    for i in range(0, len(proofs), batch_size):
        pb = proofs[i : i + batch_size]
        proof_states = await _mint_operation(
            lambda: wallet.check_proof_state(pb),
            op_name="check_proof_state",
            mint_url=str(wallet.url),
            retry_on_rate_limit=retry_on_rate_limit,
        )
        for proof, state in zip(pb, proof_states.states):
            if str(state.state) != "spent":
                _proofs.append(proof)
            else:
                _spent_proofs.append(proof)
    if _spent_proofs:
        await _mint_operation(
            lambda: wallet.set_reserved_for_send(_spent_proofs, reserved=True),
            op_name="set_reserved_spent_proofs",
            mint_url=str(wallet.url),
            retry_timeouts=False,
        )
    return _proofs


class BalanceDetail(TypedDict, total=False):
    mint_url: str
    unit: str
    wallet_balance: int
    user_balance: int
    owner_balance: int
    error: str
    error_code: str
    retry_after_seconds: float


_BALANCE_FETCH_RETRY_SECONDS = 60.0
_MINT_UNITS_CACHE_SECONDS = 300.0
_balance_fetch_failures: dict[tuple[str, str], tuple[float, str, str]] = {}
_balance_fetch_locks: dict[str, asyncio.Lock] = {}
_mint_supported_units: dict[str, tuple[float, list[str]]] = {}


async def _get_supported_mint_units(mint_url: str) -> list[str]:
    now = time.monotonic()
    cached = _mint_supported_units.get(mint_url)
    if cached is not None and now < cached[0]:
        return cached[1]

    wallet = await get_wallet(mint_url, settings.primary_mint_unit, load=False)
    keysets = await _mint_operation(
        lambda: wallet._get_keysets(),
        op_name="get_mint_keysets",
        mint_url=mint_url,
        retry_on_rate_limit=False,
    )
    units: list[str] = []
    for keyset in keysets:
        if not keyset.active or keyset.unit is None:
            continue
        unit = keyset.unit if isinstance(keyset.unit, str) else keyset.unit.name
        if unit and unit not in units:
            units.append(unit)
    if not units:
        units = [settings.primary_mint_unit]
    elif settings.primary_mint_unit in units:
        units.remove(settings.primary_mint_unit)
        units.insert(0, settings.primary_mint_unit)

    _mint_supported_units[mint_url] = (
        time.monotonic() + _MINT_UNITS_CACHE_SECONDS,
        units,
    )
    return units


def _balance_error(
    mint_url: str,
    unit: str,
    error: str,
    *,
    error_code: str,
    retry_after_seconds: float | None = None,
) -> BalanceDetail:
    detail: BalanceDetail = {
        "mint_url": mint_url,
        "unit": unit,
        "wallet_balance": 0,
        "user_balance": 0,
        "owner_balance": 0,
        "error": error,
        "error_code": error_code,
    }
    if retry_after_seconds is not None:
        detail["retry_after_seconds"] = round(max(0.0, retry_after_seconds), 2)
    return detail


async def fetch_all_balances(
    units: list[str] | None = None,
) -> tuple[list[BalanceDetail], int, int, int]:
    """
    Fetch balances for all trusted mints and units concurrently.

    Returns:
        - List of balance details for each mint/unit combination
        - Total wallet balance in sats
        - Total user balance in sats
        - Owner balance in sats (wallet - user)
    """

    async def fetch_balance(
        session: db.AsyncSession, mint_url: str, unit: str
    ) -> BalanceDetail:
        key = (mint_url, unit)
        lock = _balance_fetch_locks.setdefault(mint_url, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            failure = _balance_fetch_failures.get(key)
            if failure is not None and now < failure[0]:
                return _balance_error(
                    mint_url,
                    unit,
                    failure[1],
                    error_code=failure[2],
                    retry_after_seconds=failure[0] - now,
                )

            cooldown = _mint_cooldown_remaining(mint_url)
            if cooldown > 0:
                error_code = _mint_cooldown_reason(mint_url) or "cooldown"
                error = {
                    "rate_limited": "Mint is rate limited",
                    "unreachable": "Mint is unreachable",
                }.get(error_code, "Mint cooldown is active")
                _balance_fetch_failures[key] = (
                    now + cooldown,
                    error,
                    error_code,
                )
                return _balance_error(
                    mint_url,
                    unit,
                    error,
                    error_code=error_code,
                    retry_after_seconds=cooldown,
                )

            try:
                wallet = await get_wallet(mint_url, unit, retry_on_rate_limit=False)
                proofs = get_proofs_per_mint_and_unit(
                    wallet, mint_url, unit, not_reserved=True
                )
                proofs = await slow_filter_spend_proofs(
                    proofs, wallet, retry_on_rate_limit=False
                )
                user_balance = await db.balances_for_mint_and_unit(
                    session, mint_url, unit
                )
            except Exception as error:
                connection_failure = is_mint_connection_error(error)
                rate_limited = _is_mint_rate_limited(error)
                error_code = (
                    "rate_limited"
                    if rate_limited
                    else "unreachable"
                    if connection_failure
                    else "mint_error"
                )
                if rate_limited:
                    _MintRateGuard.get(mint_url).apply_rate_limit_cooldown(
                        _BALANCE_FETCH_RETRY_SECONDS
                    )
                elif connection_failure:
                    _MintRateGuard.get(mint_url).apply_cooldown(
                        _BALANCE_FETCH_RETRY_SECONDS, reason=error_code
                    )
                retry_delay = max(
                    _BALANCE_FETCH_RETRY_SECONDS,
                    _mint_cooldown_remaining(mint_url),
                )
                retry_at = time.monotonic() + retry_delay
                _balance_fetch_failures[key] = (retry_at, str(error), error_code)
                logger.warning(
                    "Unable to refresh mint balance",
                    extra={
                        "mint_url": mint_url,
                        "unit": unit,
                        "error": str(error),
                        "connection_failure": connection_failure,
                        "rate_limited": rate_limited,
                        "mint_cooldown_applied": connection_failure or rate_limited,
                        "retry_seconds": round(retry_delay, 2),
                    },
                )
                return _balance_error(
                    mint_url,
                    unit,
                    str(error),
                    error_code=error_code,
                    retry_after_seconds=retry_delay,
                )

            _balance_fetch_failures.pop(key, None)
            if unit == "sat":
                user_balance = user_balance // 1000
            proofs_balance = sum(proof.amount for proof in proofs)
            return {
                "mint_url": mint_url,
                "unit": unit,
                "wallet_balance": proofs_balance,
                "user_balance": user_balance,
                "owner_balance": proofs_balance - user_balance
                if proofs_balance != 0
                else 0,
            }

    # Build the set of mints to inspect. Received tokens are stored against
    # ``primary_mint`` (which defaults to a real mint even when ``cashu_mints``
    # is empty), so include it as a fallback — otherwise a node that accepts
    # payments would still report empty balances when ``cashu_mints`` is unset.
    mint_urls: list[str] = list(settings.cashu_mints)
    if settings.primary_mint and settings.primary_mint not in mint_urls:
        mint_urls.append(settings.primary_mint)

    async with db.create_session() as session:

        async def fetch_mint_balances(mint_url: str) -> list[BalanceDetail]:
            mint_units = units
            if mint_units is None:
                try:
                    mint_units = await _get_supported_mint_units(mint_url)
                except Exception as error:
                    connection_failure = is_mint_connection_error(error)
                    rate_limited = _is_mint_rate_limited(error)
                    if connection_failure:
                        _MintRateGuard.get(mint_url).apply_cooldown(
                            _BALANCE_FETCH_RETRY_SECONDS, reason="unreachable"
                        )
                    # _mint_operation already records rate-limit cooldowns.
                    # Fetching the configured unit turns a known cooldown into
                    # a structured error without another mint request.
                    mint_units = [settings.primary_mint_unit]
                    if not connection_failure and not rate_limited:
                        logger.warning(
                            "Unable to discover mint units",
                            extra={
                                "mint_url": mint_url,
                                "error": str(error),
                                "error_type": type(error).__name__,
                            },
                        )
            return list(
                await asyncio.gather(
                    *(fetch_balance(session, mint_url, unit) for unit in mint_units)
                )
            )

        grouped_details = await asyncio.gather(
            *(fetch_mint_balances(mint_url) for mint_url in mint_urls)
        )
        balance_details = [
            detail for mint_details in grouped_details for detail in mint_details
        ]

    # Calculate totals
    total_wallet_balance_sats = 0
    total_user_balance_sats = 0

    for detail in balance_details:
        if not detail.get("error"):
            # Convert to sats for total calculation
            unit = detail["unit"]
            proofs_balance_sats = (
                detail["wallet_balance"]
                if unit == "sat"
                else detail["wallet_balance"] // 1000
            )
            user_balance_sats = (
                detail["user_balance"]
                if unit == "sat"
                else detail["user_balance"] // 1000
            )

            total_wallet_balance_sats += proofs_balance_sats
            total_user_balance_sats += user_balance_sats

    owner_balance = 0
    if total_wallet_balance_sats != 0:
        owner_balance = total_wallet_balance_sats - total_user_balance_sats

    return (
        balance_details,
        total_wallet_balance_sats,
        total_user_balance_sats,
        owner_balance,
    )


async def periodic_payout() -> None:
    while True:
        await asyncio.sleep(settings.payout_interval_seconds)
        try:
            if not settings.receive_ln_address:
                continue

            # Include the primary mint even if it is not listed in cashu_mints,
            # matching fetch_all_balances(); otherwise primary-mint funds never
            # auto-payout.
            mint_urls: list[str] = list(settings.cashu_mints)
            if settings.primary_mint and settings.primary_mint not in mint_urls:
                mint_urls.append(settings.primary_mint)

            async with db.create_session() as session:
                for mint_url in mint_urls:
                    for unit in ["sat", "msat"]:
                        # Isolate failures per mint/unit so one slow or failing
                        # mint does not abort payout for every other mint/unit.
                        try:
                            wallet = await get_wallet(mint_url, unit)
                            proofs = get_proofs_per_mint_and_unit(
                                wallet, mint_url, unit, not_reserved=True
                            )
                            proofs = await slow_filter_spend_proofs(proofs, wallet)
                            await asyncio.sleep(5)
                            user_balance = await db.balances_for_mint_and_unit(
                                session, mint_url, unit
                            )
                            if unit == "sat":
                                user_balance = user_balance // 1000
                            proofs_balance = sum(proof.amount for proof in proofs)
                            available_balance = proofs_balance - user_balance
                            # Threshold is configured in sats; convert for msat wallets.
                            min_amount = (
                                settings.min_payout_sat
                                if unit == "sat"
                                else settings.min_payout_sat * 1000
                            )
                            if available_balance > min_amount:
                                amount_received = await raw_send_to_lnurl(
                                    wallet,
                                    proofs,
                                    settings.receive_ln_address,
                                    unit,
                                    amount=available_balance,
                                )
                                logger.info(
                                    "Payout sent successfully",
                                    extra={
                                        "mint_url": mint_url,
                                        "unit": unit,
                                        "balance": available_balance,
                                        "amount_received": amount_received,
                                    },
                                )
                        except Exception as e:
                            logger.error(
                                f"Error sending payout: {type(e).__name__}",
                                extra={
                                    "error": str(e),
                                    "mint_url": mint_url,
                                    "unit": unit,
                                },
                            )
        except Exception as e:
            logger.error(
                f"Error in periodic payout cycle: {type(e).__name__}",
                extra={"error": str(e)},
            )


async def periodic_refund_sweep() -> None:
    while True:
        await asyncio.sleep(60 * 60)  # every hour
        try:
            cutoff = int(time.time()) - settings.refund_sweep_ttl_seconds
            async with db.create_session() as session:
                stmt = select(db.CashuTransaction).where(
                    db.CashuTransaction.type == "out",
                    db.CashuTransaction.collected == False,  # noqa: E712
                    db.CashuTransaction.swept == False,  # noqa: E712
                    db.CashuTransaction.created_at < cutoff,
                )
                results = await session.exec(stmt)
                refunds = results.all()

                for refund in refunds:
                    try:
                        await recieve_token(refund.token)
                        refund.swept = True
                        session.add(refund)
                        logger.info(
                            "Swept uncollected refund",
                            extra={
                                "id": refund.id,
                                "amount": refund.amount,
                                "unit": refund.unit,
                            },
                        )
                    except Exception as e:
                        error_msg = str(e).lower()
                        if "already spent" in error_msg:
                            refund.collected = True
                            session.add(refund)
                            logger.info(
                                "Refund already spent (client collected), marking swept",
                                extra={
                                    "id": refund.id,
                                },
                            )
                        else:
                            logger.warning(
                                "Failed to sweep refund",
                                extra={
                                    "id": refund.id,
                                    "error": str(e),
                                },
                            )
                await session.commit()
        except Exception as e:
            logger.error(
                "Error in periodic refund sweep",
                extra={"error": str(e), "error_type": type(e).__name__},
            )


async def periodic_routstr_fee_payout() -> None:
    from .auth import (
        ROUTSTR_FEE_DEFAULT_PAYOUT,
        ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS,
        ROUTSTR_LN_ADDRESS,
    )

    if not ROUTSTR_LN_ADDRESS:
        logger.info("ROUTSTR_LN_ADDRESS not set, skipping fee payout")
        return
    while True:
        await asyncio.sleep(ROUTSTR_FEE_PAYOUT_INTERVAL_SECONDS)
        try:
            async with db.create_session() as session:
                fee = await db.get_routstr_fee(session)
                accumulated_sats = fee.accumulated_msats // 1000
                if accumulated_sats >= ROUTSTR_FEE_DEFAULT_PAYOUT:
                    wallet = await get_wallet(settings.primary_mint, "sat")
                    proofs = get_proofs_per_mint_and_unit(
                        wallet, settings.primary_mint, "sat", not_reserved=True
                    )
                    amount_received = await raw_send_to_lnurl(
                        wallet,
                        proofs,
                        ROUTSTR_LN_ADDRESS,
                        "sat",
                        amount=accumulated_sats,
                    )
                    paid_msats = accumulated_sats * 1000
                    await db.reset_routstr_fee(session, paid_msats)
                    logger.info(
                        "Routstr fee payout sent",
                        extra={
                            "accumulated_sats": accumulated_sats,
                            "amount_received": amount_received,
                        },
                    )
        except Exception as e:
            logger.error(
                f"Error in Routstr fee payout: {type(e).__name__}",
                extra={"error": str(e)},
            )


async def send_to_lnurl(amount: int, unit: str, mint: str, address: str) -> int:
    mint = await find_trusted_mint_with_funds(amount, unit, mint)
    wallet = await get_wallet(mint, unit)
    available = get_proofs_per_mint_and_unit(wallet, mint, unit, not_reserved=True)
    proofs, _ = await wallet.select_to_send(available, amount, set_reserved=True)
    return await raw_send_to_lnurl(wallet, proofs, address, unit)


# class Payment:
#     """
#     Stores all cashu payment related data
#     """

#     def __init__(self, token: str) -> None:
#         self.initial_token = token
#         amount, unit, mint_url = self.parse_token(token)
#         self.amount = amount
#         self.unit = unit
#         self.mint_url = mint_url

#         self.claimed_proofs = redeem_to_proofs(token)

#     def parse_token(self, token: str) -> tuple[int, CurrencyUnit, str]:
#         raise NotImplementedError

#     def refund_full(self) -> None:
#         raise NotImplementedError

#     def refund_partial(self, amount: int) -> None:
#         raise NotImplementedError
