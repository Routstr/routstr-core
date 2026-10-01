"""Capture how upstream responses end, for the settled outcome ledger."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
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
    context: TerminalOutcomeContext | None
    success_marker_seen: bool = False
    failure_seen: bool = False
    transport_failed: bool = False
    usage: dict[str, Any] | None = None

    def observe(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "").lower()
        status = str(event.get("status") or "").lower()
        nested_response = event.get("response")
        if isinstance(nested_response, dict):
            status = str(nested_response.get("status") or status).lower()
        # Messages report input and output usage in separate events, and a
        # completed Responses event nests its usage under "response".
        for payload in (event.get("message"), nested_response, event):
            if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
                self.usage = {**(self.usage or {}), **payload["usage"]}
        if (
            event.get("error") is not None
            or event_type in {"error", "response.failed"}
            or status in {"cancelled", "failed"}
        ):
            self.failure_seen = True
            return
        choices = event.get("choices")
        if isinstance(choices, list):
            finish_reasons = {
                str(choice.get("finish_reason") or "").lower()
                for choice in choices
                if isinstance(choice, dict) and choice.get("finish_reason") is not None
            }
            if "error" in finish_reasons:
                self.failure_seen = True
                return
            if finish_reasons - {""}:
                self.success_marker_seen = True
        # An output-limit truncation is a paid terminal response, like length.
        if event_type in {
            "response.completed",
            "response.incomplete",
            "message_stop",
        } or status in {"completed", "incomplete"}:
            self.success_marker_seen = True
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("stop_reason") not in (None, ""):
            self.success_marker_seen = True

    def mark_success(self) -> None:
        self.success_marker_seen = True

    def mark_transport_failure(self) -> None:
        self.transport_failed = True

    def settlement_context(
        self, *, require_success: bool = False
    ) -> TerminalOutcomeContext | None:
        return self.context


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


async def track_generic_terminal_stream(
    stream: AsyncIterator[bytes], state: TerminalOutcomeState
) -> AsyncGenerator[bytes, None]:
    try:
        async for chunk in stream:
            yield chunk
        state.mark_success()
    except BaseException:
        state.mark_transport_failure()
        raise


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
            state.mark_success()
            continue
        try:
            parsed = json.loads(payload)
        except ValueError:
            # Bytes cut inside a character raise UnicodeDecodeError, not JSONDecodeError.
            if final:
                state.mark_transport_failure()
            continue
        if isinstance(parsed, dict):
            state.observe(parsed)
    return pending
