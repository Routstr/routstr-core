"""Capture upstream usage and outcome contexts for the settled outcome ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..core.terminal_outcomes import (
    TerminalOutcomeContext,
    cashu_retained_msats,
    record_terminal_outcome,
)
from ..payment.cost_calculation import CostMetadata, cost_field
from ..payment.usage import UsageFieldPresence, usage_field_presence

if TYPE_CHECKING:
    from ..payment.models import Model


@dataclass
class TerminalOutcomeState:
    usage: dict[str, Any] | None = None

    def observe(self, event: dict[str, Any]) -> None:
        # Messages report input and output usage in separate events, and a
        # completed Responses event nests its usage under "response".
        for payload in (event.get("message"), event.get("response"), event):
            if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
                self.usage = {**(self.usage or {}), **payload["usage"]}


def terminal_outcome_context(
    request_id: str | None, model_obj: Model | None
) -> TerminalOutcomeContext:
    return TerminalOutcomeContext(
        outcome_id=request_id,
        model_identifier=(model_obj.canonical_slug or model_obj.id)
        if model_obj is not None
        else None,
        served_model_identifier=(model_obj.forwarded_model_id or model_obj.id)
        if model_obj is not None
        else None,
    )


def event_usage_presence(event: object) -> UsageFieldPresence:
    if not isinstance(event, dict):
        return UsageFieldPresence()
    presence = usage_field_presence(event.get("usage"))
    for key in ("message", "response"):
        nested = event.get(key)
        if isinstance(nested, dict):
            presence = presence.merged(usage_field_presence(nested.get("usage")))
    return presence


def record_x_cashu_terminal_outcome(
    context: TerminalOutcomeContext,
    cost_data: CostMetadata | None,
    *,
    amount: int,
    unit: str,
    refund_amount: int = 0,
    usage: object = None,
) -> None:
    revenue_msats = cashu_retained_msats(amount, unit, refund_amount)
    if revenue_msats is None:
        return
    metadata = {}
    if cost_data is not None:
        for name in (
            "input_source",
            "output_source",
            "cache_read_source",
            "cache_creation_source",
            "pricing_source",
        ):
            value = (
                cost_data.get(name)
                if isinstance(cost_data, dict)
                else getattr(cost_data, name, None)
            )
            if isinstance(value, str):
                metadata[name] = value
    context = replace(context, **metadata)
    if usage is not None:
        # Stats keep what upstream reported, even where billing did not parse it,
        # and billing's labels stay on the counts upstream left out.
        billed = UsageFieldPresence(
            input_source=context.input_source or "missing",
            output_source=context.output_source or "missing",
            cache_read_source=context.cache_read_source or "missing",
            cache_creation_source=context.cache_creation_source or "missing",
        )
        presence = billed.merged(usage_field_presence(usage))
        context = replace(context, **presence.sources_dict())
    counted: CostMetadata = cost_data if cost_data is not None else {}
    record_terminal_outcome(
        context,
        input_tokens=int(cost_field(counted, "input_tokens")),
        output_tokens=int(cost_field(counted, "output_tokens")),
        cache_read_input_tokens=int(cost_field(counted, "cache_read_input_tokens")),
        cache_creation_input_tokens=int(
            cost_field(counted, "cache_creation_input_tokens")
        ),
        revenue_msats=revenue_msats,
        usage=usage,
    )


def observe_terminal_sse_bytes(
    state: TerminalOutcomeState,
    buffered: bytes,
    chunk: bytes = b"",
    *,
    final: bool = False,
) -> bytes:
    """Observe complete SSE events while preserving a split trailing event."""
    pending = (buffered + chunk).replace(b"\r\n", b"\n")
    events: list[bytes] = []
    while b"\n\n" in pending:
        event, pending = pending.split(b"\n\n", 1)
        events.append(event)
    if final and pending.strip():
        events.append(pending)
        pending = b""

    for event in events:
        lines = event.split(b"\n")
        # A Routstr upstream appends its own cost summary, with cached tokens
        # folded into input_tokens; it is not provider usage.
        if any(
            line.startswith(b"event:") and line[len(b"event:") :].strip() == b"cost"
            for line in lines
        ):
            continue
        data_lines = [
            line[len(b"data:") :].lstrip(b" ")
            for line in lines
            if line.startswith(b"data:")
        ]
        if not data_lines:
            continue
        payload = b"\n".join(data_lines)
        if payload.strip() == b"[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except ValueError:
            # Bytes cut inside a character raise UnicodeDecodeError, not JSONDecodeError.
            continue
        if isinstance(parsed, dict):
            state.observe(parsed)
    return pending
