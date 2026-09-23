from __future__ import annotations

import time
from dataclasses import dataclass

from .logging import get_logger
from .terminal_outcome_writer import (
    TerminalOutcomeWriter,
    _QueuedOutcome,
    _utc_day_from_ms,
    _valid_nonnegative_int,
)

logger = get_logger(__name__)

# Far above any real request, and low enough that a day's sums stay JSON-safe.
_MAX_TOKENS = 2**31 - 1
_MAX_REVENUE_MSATS = 2**40


@dataclass(frozen=True)
class TerminalOutcomeContext:
    outcome_id: str | None
    model_identifier: str | None

    served_model_identifier: str | None = None
    pricing_source: str | None = None
    input_source: str | None = None
    output_source: str | None = None
    cache_read_source: str | None = None
    cache_creation_source: str | None = None


terminal_outcome_writer = TerminalOutcomeWriter()


def record_terminal_outcome(
    context: TerminalOutcomeContext,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
    revenue_msats: int,
    terminal_at_ms: int | None = None,
    usage: object = None,
) -> None:
    """Submit a settled outcome without awaiting storage or raising.

    ``usage`` is the raw upstream usage. It replaces the token counts and is
    parsed here, so a malformed value can only mark a gap.
    """
    try:
        if usage is not None:
            from ..payment.usage import NormalizedUsage, normalize_usage

            counted = normalize_usage(usage) or NormalizedUsage()
            input_tokens = counted.input_tokens
            output_tokens = counted.output_tokens
            cache_read_input_tokens = counted.cache_read_tokens
            cache_creation_input_tokens = counted.cache_write_tokens
        sources = {
            name + "_source": getattr(context, name + "_source") or "missing"
            for name in ("input", "output", "cache_read", "cache_creation")
        }
        tokens = (
            input_tokens,
            output_tokens,
            cache_read_input_tokens,
            cache_creation_input_tokens,
        )
        timestamp = (
            terminal_at_ms if terminal_at_ms is not None else int(time.time() * 1000)
        )
        terminal_day = _utc_day_from_ms(timestamp)
        if (
            not context.outcome_id
            or any(
                source not in {"reported", "estimated", "missing"}
                for source in sources.values()
            )
            or any(not _valid_nonnegative_int(value, _MAX_TOKENS) for value in tokens)
            or not _valid_nonnegative_int(revenue_msats, _MAX_REVENUE_MSATS)
            or not _valid_nonnegative_int(timestamp)
        ):
            terminal_outcome_writer.declare_loss(
                "invalid settled terminal outcome", terminal_day
            )
            return
        terminal_outcome_writer.submit(
            _QueuedOutcome(
                outcome_id=context.outcome_id,
                terminal_at_ms=timestamp,
                terminal_day=terminal_day,
                model_identifier=context.model_identifier,
                served_model_identifier=context.served_model_identifier,
                pricing_source=context.pricing_source,
                **sources,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                revenue_msats=revenue_msats,
            )
        )
    except BaseException:
        try:
            terminal_outcome_writer.declare_loss("terminal outcome submission raised")
            logger.critical("Terminal outcome submission failed", exc_info=True)
        except BaseException:
            pass


def mark_terminal_outcome_loss(reason: str) -> None:
    try:
        terminal_outcome_writer.declare_loss(reason)
    except BaseException:
        pass


def cashu_retained_msats(amount: int, unit: str, refund_amount: int = 0) -> int | None:
    try:
        if not _valid_nonnegative_int(amount) or not _valid_nonnegative_int(
            refund_amount
        ):
            raise ValueError("Cashu amounts must be nonnegative integers")
        if refund_amount > amount:
            raise ValueError("Cashu refund exceeds redeemed amount")
        if unit == "msat":
            multiplier = 1
        elif unit == "sat":
            multiplier = 1000
        else:
            raise ValueError(f"Unsupported Cashu unit: {unit}")
        return (amount - refund_amount) * multiplier
    except Exception:
        mark_terminal_outcome_loss("invalid Cashu retained value")
        return None


async def start_terminal_outcome_writer(*, serving: bool = False) -> bool:
    return await terminal_outcome_writer.start(serving=serving)


async def stop_terminal_outcome_writer(
    *, timeout: float = 5.0, close_coverage: bool = False
) -> bool:
    return await terminal_outcome_writer.stop(
        timeout=timeout, close_coverage=close_coverage
    )
