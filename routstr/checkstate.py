"""Bounded adaptive batching for read-only NUT-07 requests, never mutations."""

import asyncio
import time

import httpx
from cashu.core.base import Proof, ProofSpentState, ProofState
from cashu.core.models import PostCheckStateRequest
from cashu.wallet.wallet import Wallet

from .core.logging import get_logger
from .mint import MINT_TRANSPORT_COOLDOWN_SECONDS, MintRateGuard, run_mint_operation

logger = get_logger(__name__)
_SDK_BATCH_LIMIT = PostCheckStateRequest.model_json_schema()["properties"]["Ys"][
    "maxItems"
]
_LEARNED_TTL = 24 * 60 * 60
# Fixed scan bounds: adaptive halving makes the start size near irrelevant, and
# the deadline/request budget are safety limits, not tuning knobs.
_DEFAULT_BATCH_SIZE = _SDK_BATCH_LIMIT
_SCAN_TIMEOUT_SECONDS = 60
_MAX_REQUESTS = 128
_learned_sizes: dict[str, tuple[int, float]] = {}


async def filter_unspent_proofs(
    proofs: list[Proof], wallet: Wallet, *, retry_on_rate_limit: bool = True
) -> list[Proof]:
    if not proofs:
        return []
    mint_url = str(wallet.url)
    key = mint_url.rstrip("/")
    configured = _DEFAULT_BATCH_SIZE
    learned, expires = _learned_sizes.get(key, (configured, 0.0))
    batch_size = min(configured, learned) if expires > time.monotonic() else configured
    unspent: list[Proof] = []
    spent: list[Proof] = []
    offset = 0
    requests = 0

    async def check_batch() -> tuple[list[Proof], list[ProofState]]:
        nonlocal batch_size, requests
        # Size fallback stays inside the rate guard's operation. A recoverable
        # 500 during a cooldown probe must not open another cooldown first.
        while True:
            batch = proofs[offset : offset + batch_size]
            if requests >= _MAX_REQUESTS:
                raise ValueError("Proof-state request budget exhausted")
            requests += 1
            try:
                response = await wallet.check_proof_state(batch)
            except httpx.HTTPStatusError as error:
                # A proxy 500 can mean a body limit (#761), but is not proof of
                # one. Diagnostic retries are safe here because this is a read.
                if error.response.status_code not in {413, 500} or len(batch) == 1:
                    logger.warning(
                        "Proof-state request failed; scan aborted",
                        extra={
                            "mint_url": mint_url,
                            "endpoint": "/v1/checkstate",
                            "status": error.response.status_code,
                            "content_type": error.response.headers.get("content-type"),
                            "request_bytes": error.request.headers.get(
                                "content-length"
                            ),
                            "proof_count": len(batch),
                            "requests": requests,
                        },
                    )
                    raise
                batch_size = max(1, len(batch) // 2)
                logger.warning(
                    "Retrying proof-state check with a smaller batch",
                    extra={
                        "mint_url": mint_url,
                        "endpoint": "/v1/checkstate",
                        "status": error.response.status_code,
                        "content_type": error.response.headers.get("content-type"),
                        "request_bytes": error.request.headers.get("content-length"),
                        "proof_count": len(batch),
                        "next_batch_size": batch_size,
                        "requests": requests,
                    },
                )
                continue
            states = response.states
            if len(states) != len(batch) or any(
                state.Y != proof.Y for proof, state in zip(batch, states)
            ):
                raise ValueError("Invalid proof-state response: count or Y mismatch")
            if any(state.state not in set(ProofSpentState) for state in states):
                raise ValueError("Invalid proof-state response: unknown state")
            return batch, states

    # Bound the entire scan, including retries and cooldown waits.
    deadline = asyncio.timeout(_SCAN_TIMEOUT_SECONDS)
    try:
        async with deadline:
            while offset < len(proofs):
                batch, states = await run_mint_operation(
                    check_batch,
                    op_name="check_proof_state",
                    mint_url=mint_url,
                    retry_on_rate_limit=retry_on_rate_limit,
                )
                if batch_size < configured:
                    _learned_sizes[key] = (batch_size, time.monotonic() + _LEARNED_TTL)
                for proof, state in zip(batch, states):
                    if state.state == ProofSpentState.unspent:
                        unspent.append(proof)
                    elif state.state == ProofSpentState.spent:
                        spent.append(proof)
                    # Retain PENDING proofs without making them spendable.
                offset += len(batch)
            if spent:
                await wallet.set_reserved_for_send(spent, reserved=True)
    except TimeoutError:
        if deadline.expired():
            MintRateGuard.get(mint_url).apply_cooldown(
                MINT_TRANSPORT_COOLDOWN_SECONDS, reason="transport"
            )
        raise
    return unspent
