"""Bounded completion probes shared by ordinary and cache certification."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

PROBE_TIMEOUT_SECONDS = 15.0
PROBE_TOKEN_BUDGETS = (32, 128, 512, 2048)
PROBE_MAX_TOKENS = PROBE_TOKEN_BUDGETS[0]
# One field correction plus the four bounded budgets, never general retries.
PROBE_MAX_ATTEMPTS = len(PROBE_TOKEN_BUDGETS) + 1


def wants_max_completion_tokens(status: int | None, payload: Any) -> bool:
    if status != 400 or payload is None:
        return False
    text = json.dumps(payload, default=str).lower()
    return "max_completion_tokens" in text and (
        "unsupported" in text
        and "max_tokens" in text
        or "use 'max_completion_tokens'" in text
    )


def next_probe_budget(status: int | None, payload: Any, current: int) -> int | None:
    if status not in (400, 422) or payload is None:
        return None
    text = json.dumps(payload, default=str).lower()
    minimum = re.search(
        r"(?:max_tokens|max_completion_tokens|max_output_tokens)"
        r".{0,80}?(?:at least|minimum(?: of)?|>=|greater than or equal to)"
        r"\D{0,12}(\d+)",
        text,
    )
    required = int(minimum[1]) if minimum else None
    if required is not None:
        if required <= current:
            return None
    elif not (
        "max_tokens or model output limit was reached" in text
        or (
            any(name in text for name in ("max_tokens", "max_completion_tokens"))
            and any(
                hint in text for hint in ("higher max", "exhausted", "increase max")
            )
        )
    ):
        return None
    return next(
        (n for n in PROBE_TOKEN_BUDGETS if n > current and n >= (required or 0)),
        None,
    )


def diagnostic_text(value: Any, headers: dict[str, str], limit: int = 1000) -> str:
    """Redact known request credentials before bounding diagnostic text."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for name, secret in headers.items():
        if name.lower() in {"authorization", "api-key", "x-api-key"} and secret:
            text = text.replace(secret, "[redacted]")
            if name.lower() == "authorization" and " " in secret:
                text = text.replace(secret.split(" ", 1)[1], "[redacted]")
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class CompletionProbe:
    status: int | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None
    latency_ms: float = 0.0
    token_limit_field: str = "max_tokens"
    token_limit: int = PROBE_MAX_TOKENS
    attempts: list[dict[str, Any]] = field(default_factory=list)


async def send_completion(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    params: dict[str, str],
    timeout: float,
) -> CompletionProbe:
    """Adapt only recognized token-limit rejections, preserving every call.

    The body is already provider-shaped. This policy is exclusive to synthetic
    certification requests; user request budgets are never changed here.
    """
    body = dict(body)
    result = CompletionProbe()
    for _ in range(PROBE_MAX_ATTEMPTS):
        token_field = (
            "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
        )
        budget = int(body[token_field])
        if not 1 <= budget <= PROBE_TOKEN_BUDGETS[-1]:
            raise ValueError("certification token budget is outside the probe ceiling")
        result.token_limit_field = token_field
        result.token_limit = budget
        result.status, result.payload, result.error = None, None, None
        diagnostic: Any = None
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                response = await client.post(
                    url, json=body, headers=headers, params=params
                )
            result.status = response.status_code
            diagnostic = response.text
            try:
                payload = response.json()
            except Exception as exc:  # noqa: BLE001 - decoding failure is evidence
                result.error = f"{type(exc).__name__}: {exc}"
            else:
                if isinstance(payload, dict):
                    result.payload = payload
                else:
                    result.error = (
                        f"expected a JSON object, got {type(payload).__name__}"
                    )
        except Exception as exc:  # noqa: BLE001 - cancellation still propagates
            result.error = f"{type(exc).__name__}: {exc}"
        result.latency_ms = round((time.monotonic() - started) * 1000, 2)
        if result.error:
            result.error = diagnostic_text(result.error, headers)
        result.attempts.append(
            {
                "status_code": result.status,
                "latency_ms": result.latency_ms,
                "token_limit_field": token_field,
                "token_limit": budget,
                "error": result.error,
                "body": diagnostic_text(diagnostic, headers)
                if diagnostic is not None
                and (result.error or not 200 <= (result.status or 0) < 300)
                else None,
            }
        )
        if token_field == "max_tokens" and wants_max_completion_tokens(
            result.status, result.payload
        ):
            body["max_completion_tokens"] = body.pop("max_tokens")
            continue
        next_budget = next_probe_budget(result.status, result.payload, budget)
        if next_budget is None:
            break
        body[token_field] = next_budget
    return result
