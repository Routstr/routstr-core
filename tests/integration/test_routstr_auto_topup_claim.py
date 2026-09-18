"""Real-database tests for the Routstr-to-Routstr auto top-up spend bound.

The bound has to survive a process restart and concurrent workers, so these
run against actual SQL instead of mocked sessions: an in-memory counter would
pass a mocked test and still let a non-crediting peer drain the wallet.
"""

import importlib
import time
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

from routstr.core.db import CashuTransaction, UpstreamProviderRow, create_session
from routstr.upstream import auto_topup as auto_topup_module
from routstr.upstream.auto_topup import (
    ROUTSTR_MAX_DAILY_TOPUP_SATS,
    ROUTSTR_MAX_TOPUP_FAILURES,
    ROUTSTR_PHASE_BACKOFF,
    ROUTSTR_PHASE_HALTED,
    ROUTSTR_PHASE_SENT,
    _check_and_topup,
    _claim_routstr_topup,
    _parse_routstr_request_id,
    _persist_routstr_token_and_mark_sent,
    _routstr_spent_last_24h_sats,
    _routstr_state_id_for_provider,
    get_routstr_auto_topup_state,
    release_routstr_auto_topup_state,
)

pytestmark = pytest.mark.asyncio

TOPUP_SATS = 50


async def _seed_provider(provider_id: int = 1) -> UpstreamProviderRow:
    import json

    row = UpstreamProviderRow(
        id=provider_id,
        slug=f"peer-{provider_id}",
        provider_type="routstr",
        base_url="https://peer.test",
        api_key="secret",
        enabled=True,
        provider_settings=json.dumps(
            {
                "auto_topup": True,
                "topup_threshold": 1,
                "topup_amount_limit": TOPUP_SATS,
                "topup_mint_url": "https://mint.test",
            }
        ),
    )
    async with create_session() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


def _peer(balance: float, *, topup: object = None) -> MagicMock:
    provider = MagicMock()
    provider.get_balance = AsyncMock(return_value=balance)
    provider.topup = AsyncMock(return_value=topup or {"balance": balance})
    return provider


def _patch_wallet(module: Any, peer: MagicMock, token: str) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(
        patch.object(module.RoutstrUpstreamProvider, "from_db_row", return_value=peer)
    )
    stack.enter_context(
        patch.object(
            module, "send_token_from_owner_locked", AsyncMock(return_value=token)
        )
    )
    stack.enter_context(
        patch.object(module, "token_mint_url", return_value="https://mint.test")
    )
    return stack


async def _claim_state(provider_id: int = 1) -> CashuTransaction | None:
    async with create_session() as session:
        return await session.get(
            CashuTransaction, _routstr_state_id_for_provider(provider_id)
        )


async def _sent_tokens() -> list[CashuTransaction]:
    async with create_session() as session:
        return list(
            (
                await session.exec(
                    select(CashuTransaction).where(
                        CashuTransaction.source == "auto_topup"
                    )
                )
            ).all()
        )


async def test_second_worker_cannot_claim_while_the_first_holds_one(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    assert await _claim_routstr_topup(row, expected_sats=TOPUP_SATS) is not None
    assert await _claim_routstr_topup(row, expected_sats=TOPUP_SATS) is None


async def test_token_and_sent_claim_roll_back_together_on_commit_failure(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    operation_id = await _claim_routstr_topup(row, expected_sats=TOPUP_SATS)
    assert operation_id is not None

    with patch(
        "sqlmodel.ext.asyncio.session.AsyncSession.commit",
        new=AsyncMock(side_effect=RuntimeError("commit failed")),
    ):
        with pytest.raises(RuntimeError, match="commit failed"):
            await _persist_routstr_token_and_mark_sent(
                row,
                operation_id,
                expected_sats=TOPUP_SATS,
                token="cashu-token-atomic",
                amount=TOPUP_SATS,
                mint_url="https://mint.test",
            )

    claim = _parse_routstr_request_id((await _claim_state()).request_id)  # type: ignore[union-attr]
    assert claim is not None and claim.phase != ROUTSTR_PHASE_SENT
    assert await _sent_tokens() == []


async def test_token_is_persisted_before_it_reaches_the_peer(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    seen: list[CashuTransaction] = []

    async def _record_then_accept(token: str) -> dict:
        seen.extend(await _sent_tokens())
        return {"balance": TOPUP_SATS}

    peer = _peer(0.0)
    peer.topup = AsyncMock(side_effect=_record_then_accept)

    with _patch_wallet(auto_topup_module, peer, "cashu-token-1"):
        await _check_and_topup(row)

    assert [tx.token for tx in seen] == ["cashu-token-1"]
    assert seen[0].collected is False
    assert (await _sent_tokens())[0].collected is True


async def test_untracked_token_is_returned_and_never_sent(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    peer = _peer(0.0)

    with (
        _patch_wallet(auto_topup_module, peer, "cashu-token-1"),
        patch.object(
            auto_topup_module,
            "_persist_routstr_token_and_mark_sent",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch.object(
            auto_topup_module, "release_token_reservation", AsyncMock()
        ) as reclaim,
    ):
        await _check_and_topup(row)

    reclaim.assert_awaited_once_with("cashu-token-1")
    peer.topup.assert_not_awaited()
    # Nothing left the wallet, so the slot must be free again immediately.
    state = await _claim_state()
    assert state is not None and state.swept is True


async def test_failed_topup_keeps_token_uncollected_and_suppresses_new_claims(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    peer = _peer(0.0, topup={"error": "rejected"})

    with _patch_wallet(auto_topup_module, peer, "cashu-token-1"):
        await _check_and_topup(row)

    tokens = await _sent_tokens()
    assert len(tokens) == 1
    assert tokens[0].collected is False

    claim = _parse_routstr_request_id((await _claim_state()).request_id)  # type: ignore[union-attr]
    assert claim is not None and claim.phase == ROUTSTR_PHASE_SENT

    peer.topup.reset_mock()
    with _patch_wallet(auto_topup_module, peer, "cashu-token-2"):
        await _check_and_topup(row)

    peer.topup.assert_not_awaited()
    assert len(await _sent_tokens()) == 1


async def test_claim_blocks_a_restarted_process(patched_db_engine: Any) -> None:
    row = await _seed_provider()
    peer = _peer(0.0, topup={"error": "rejected"})

    with _patch_wallet(auto_topup_module, peer, "cashu-token-1"):
        await _check_and_topup(row)

    # A fresh module drops every process-local variable; only a durable row
    # can still stop the next payment.
    reloaded = importlib.reload(auto_topup_module)
    try:
        peer.topup.reset_mock()
        with _patch_wallet(reloaded, peer, "cashu-token-2"):
            await reloaded._check_and_topup(row)
        peer.topup.assert_not_awaited()
    finally:
        importlib.reload(auto_topup_module)

    assert len(await _sent_tokens()) == 1


async def _uncredited_attempt(
    row: UpstreamProviderRow, peer: MagicMock, token: str
) -> None:
    """One full payment attempt against a peer that never credits it."""
    with _patch_wallet(auto_topup_module, peer, token):
        await _check_and_topup(row)
    await _expire_claim()
    with _patch_wallet(auto_topup_module, peer, f"{token}-retry"):
        await _check_and_topup(row)
    await _expire_claim()


async def test_non_crediting_peer_is_halted_after_repeated_failures(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    peer = _peer(0.0)

    for attempt in range(ROUTSTR_MAX_TOPUP_FAILURES):
        await _uncredited_attempt(row, peer, f"cashu-token-{attempt}")

    claim = _parse_routstr_request_id((await _claim_state()).request_id)  # type: ignore[union-attr]
    assert claim is not None and claim.phase == ROUTSTR_PHASE_HALTED

    peer.topup.reset_mock()
    with _patch_wallet(auto_topup_module, peer, "cashu-token-after-halt"):
        await _check_and_topup(row)
    peer.topup.assert_not_awaited()
    assert len(await _sent_tokens()) == ROUTSTR_MAX_TOPUP_FAILURES


async def _expire_claim(provider_id: int = 1) -> None:
    """Age the claim's deadline so the reconciler treats it as timed out."""
    async with create_session() as session:
        state = await session.get(
            CashuTransaction, _routstr_state_id_for_provider(provider_id)
        )
        assert state is not None and state.request_id is not None
        claim = _parse_routstr_request_id(state.request_id)
        assert claim is not None
        state.request_id = auto_topup_module._routstr_request_id(
            claim.operation_id,
            int(time.time()) - 1,
            claim.phase,
            claim.expected_sats,
            claim.failures,
        )
        session.add(state)
        await session.commit()


async def test_crediting_peer_releases_the_claim_for_a_later_topup(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    peer = _peer(0.0)

    with _patch_wallet(auto_topup_module, peer, "cashu-token-1"):
        await _check_and_topup(row)

    tokens = await _sent_tokens()
    assert len(tokens) == 1 and tokens[0].collected is True

    peer.get_balance = AsyncMock(return_value=float(TOPUP_SATS))
    with _patch_wallet(auto_topup_module, peer, "cashu-token-2"):
        await _check_and_topup(row)

    state = await _claim_state()
    assert state is not None and state.collected is True


async def test_rolling_budget_refuses_a_topup_that_would_exceed_it(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    async with create_session() as session:
        session.add(
            CashuTransaction(
                id="prior-spend",
                token="cashu-prior",
                amount=ROUTSTR_MAX_DAILY_TOPUP_SATS,
                unit="sat",
                type="out",
                source="auto_topup",
            )
        )
        await session.commit()

    assert await _routstr_spent_last_24h_sats() == ROUTSTR_MAX_DAILY_TOPUP_SATS

    peer = _peer(0.0)
    with _patch_wallet(auto_topup_module, peer, "cashu-token-1"):
        await _check_and_topup(row)

    peer.topup.assert_not_awaited()
    assert await _claim_state() is None


async def test_admin_release_is_fenced_on_the_state_it_reviewed(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    peer = _peer(0.0, topup={"error": "rejected"})
    with _patch_wallet(auto_topup_module, peer, "cashu-token-1"):
        await _check_and_topup(row)

    state = await get_routstr_auto_topup_state(1)
    assert state["active"] is True
    assert state["phase"] == ROUTSTR_PHASE_SENT

    stale = await release_routstr_auto_topup_state(
        1, state_token="routstr:other:0:sent:0:0"
    )
    assert stale.released is False and stale.reason == "stale_state"

    released = await release_routstr_auto_topup_state(
        1, state_token=str(state["state_token"])
    )
    assert released.released is True

    peer.topup.reset_mock()
    with _patch_wallet(auto_topup_module, peer, "cashu-token-2"):
        await _check_and_topup(row)
    peer.topup.assert_awaited_once()


async def test_backoff_suppresses_retries_until_its_deadline(
    patched_db_engine: Any,
) -> None:
    row = await _seed_provider()
    peer = _peer(0.0)

    with _patch_wallet(auto_topup_module, peer, "cashu-token-1"):
        await _check_and_topup(row)
    await _expire_claim()

    # First reconciliation after the lease: the peer never credited, so the
    # claim moves to backoff rather than paying again immediately.
    peer.topup.reset_mock()
    with _patch_wallet(auto_topup_module, peer, "cashu-token-2"):
        await _check_and_topup(row)
    peer.topup.assert_not_awaited()

    claim = _parse_routstr_request_id((await _claim_state()).request_id)  # type: ignore[union-attr]
    assert claim is not None and claim.phase == ROUTSTR_PHASE_BACKOFF
    assert claim.deadline > int(time.time())
