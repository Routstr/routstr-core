from unittest.mock import AsyncMock, Mock

import pytest
from cashu.core.base import MeltQuoteState, ProofSpentState

from routstr.wallet import (
    TokenConsumedError,
    _confirm_melt_paid,
    _reconcile_ambiguous_melt,
)


@pytest.mark.asyncio
async def test_paid_quote_is_authoritative_when_proof_lookup_would_fail() -> None:
    wallet = Mock(
        url="http://source-mint:3338",
        get_melt_quote=AsyncMock(return_value=Mock(state=MeltQuoteState.paid)),
        check_proof_state=AsyncMock(side_effect=RuntimeError("proof API unavailable")),
    )

    assert await _reconcile_ambiguous_melt(wallet, "quote-1", [Mock()]) is True
    wallet.check_proof_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_snapshot_unpaid_unspent_remains_non_retryable() -> None:
    wallet = Mock(
        url="http://source-mint:3338",
        get_melt_quote=AsyncMock(return_value=Mock(state=MeltQuoteState.unpaid)),
        check_proof_state=AsyncMock(
            return_value=Mock(states=[Mock(state=ProofSpentState.unspent)])
        ),
    )

    with pytest.raises(TokenConsumedError, match="ambiguous"):
        await _reconcile_ambiguous_melt(wallet, "quote-2", [Mock()])


@pytest.mark.asyncio
async def test_successful_pending_melt_response_requires_reconciliation() -> None:
    wallet = Mock(
        url="http://source-mint:3338",
        get_melt_quote=AsyncMock(return_value=Mock(state=MeltQuoteState.pending)),
        check_proof_state=AsyncMock(
            return_value=Mock(states=[Mock(state=ProofSpentState.pending)])
        ),
    )

    with pytest.raises(TokenConsumedError, match="ambiguous"):
        await _confirm_melt_paid(
            wallet,
            "quote-pending",
            [Mock()],
            Mock(state=MeltQuoteState.pending),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quote_state", "proof_state"),
    [
        (MeltQuoteState.pending, ProofSpentState.pending),
        (MeltQuoteState.unpaid, ProofSpentState.spent),
        (MeltQuoteState.unpaid, ProofSpentState.pending),
    ],
)
async def test_ambiguous_or_consumed_melt_is_never_reported_unspent(
    quote_state: MeltQuoteState, proof_state: ProofSpentState
) -> None:
    wallet = Mock(
        url="http://source-mint:3338",
        get_melt_quote=AsyncMock(return_value=Mock(state=quote_state)),
        check_proof_state=AsyncMock(
            return_value=Mock(states=[Mock(state=proof_state)])
        ),
    )

    with pytest.raises(TokenConsumedError, match="reconciliation required"):
        await _reconcile_ambiguous_melt(wallet, "quote-3", [Mock()])


@pytest.mark.asyncio
async def test_failed_melt_reconciliation_is_non_retryable() -> None:
    wallet = Mock(
        url="http://source-mint:3338",
        get_melt_quote=AsyncMock(side_effect=RuntimeError("mint unavailable")),
        check_proof_state=AsyncMock(),
    )

    with pytest.raises(TokenConsumedError, match="outcome is unknown"):
        await _reconcile_ambiguous_melt(wallet, "quote-4", [Mock()])
