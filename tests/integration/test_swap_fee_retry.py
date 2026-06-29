"""
Integration tests for reactive swap fee retries via the wallet topup endpoint.

Foreign-mint tokens are swapped to the primary mint using the foreign mint's
melt quote, whose fee_reserve is a non-binding estimate (NUT-05): the mint may
demand more when re-quoting or at melt execution. These tests cover the
endpoint behaviour in those cases:

1. The mint demands one sat more at melt time than every quote reported
   (the mint.cubabitcoin.org incident): the swap retries with a smaller
   invoice and the topup succeeds, crediting the recomputed amount.
2. The real melt quote reports a higher fee_reserve than the estimate: the
   swap re-quotes from the observed fee and the topup succeeds.
3. The mint escalates its fee demands on every attempt: the retry budget is
   exhausted and the endpoint returns 400 with a clear error (never 500),
   without ever executing a melt.
"""

from collections.abc import Callable
from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import AsyncClient, Response

from routstr.core.settings import settings

# Captured at collection time, before the integration_app fixture replaces it
# with the testmint stub that bypasses swapping (see conftest.py).
from routstr.wallet import recieve_token as _real_recieve_token

PRIMARY_MINT = "http://primary:3338"


def _make_swap_mocks(
    token_amount: int,
    fee_reserves: list[int],
    input_fees: int = 0,
    mint_url: str = "http://foreign-mint:3338",
) -> tuple[Mock, Mock, Mock]:
    """Return (token, token_wallet, primary_wallet) mocks that act like a mint.

    Mint quotes pass the requested amount through their ``request`` field and
    melt quotes echo that amount back, so the mocks stay consistent for
    whatever amounts the implementation requests. ``fee_reserves`` supplies the
    fee_reserve of each successive melt quote (the first serves the estimation
    pass); requesting more quotes than provided fails the test.
    """
    mock_token = Mock()
    mock_token.mint = mint_url
    mock_token.unit = "sat"
    mock_token.amount = token_amount
    mock_token.keysets = ["keyset1"]
    mock_token.proofs = [Mock(amount=token_amount)]

    mock_token_wallet = Mock()
    mock_token_wallet.load_mint = AsyncMock()
    mock_token_wallet.load_proofs = AsyncMock()
    mock_token_wallet.get_fees_for_proofs = Mock(return_value=input_fees)

    mock_primary_wallet = Mock()
    mock_primary_wallet.load_mint = AsyncMock()
    mock_primary_wallet.load_proofs = AsyncMock()
    mock_primary_wallet.available_balance = Mock(amount=0)
    mock_primary_wallet.mint = AsyncMock(return_value=Mock())

    fees = iter(fee_reserves)

    def _next_fee() -> int:
        try:
            return next(fees)
        except StopIteration:
            raise AssertionError(
                "more melt quotes requested than fee_reserves provided"
            ) from None

    mock_primary_wallet.request_mint = AsyncMock(
        side_effect=lambda amount: Mock(quote=f"mint_quote_{amount}", request=amount)
    )
    mock_token_wallet.melt_quote = AsyncMock(
        side_effect=lambda invoice: Mock(
            quote=f"melt_quote_{invoice}", amount=invoice, fee_reserve=_next_fee()
        )
    )
    mock_token_wallet.melt = AsyncMock(return_value=Mock())

    return mock_token, mock_token_wallet, mock_primary_wallet


def _wallet_router(primary_wallet: Mock, token_wallet: Mock) -> Callable[..., Mock]:
    """Route get_wallet calls to the primary or foreign wallet mock by URL."""

    def fake_get_wallet(mint_url: str, unit: str = "sat", load: bool = True) -> Mock:
        return primary_wallet if mint_url == PRIMARY_MINT else token_wallet

    return fake_get_wallet


async def _post_topup(
    client: AsyncClient,
    mock_token: Mock,
    token_wallet: Mock,
    primary_wallet: Mock,
) -> Response:
    """POST /v1/wallet/topup with the swap layer mocked at the mint boundary.

    The conftest's testmint stub for recieve_token is swapped back for the
    real implementation so the request exercises the actual swap path.
    """
    with patch("routstr.wallet.recieve_token", _real_recieve_token):
        with patch(
            "routstr.wallet.deserialize_token_from_string", return_value=mock_token
        ):
            with patch(
                "routstr.wallet.get_wallet",
                side_effect=_wallet_router(primary_wallet, token_wallet),
            ):
                with patch.object(settings, "primary_mint", PRIMARY_MINT):
                    with patch.object(settings, "primary_mint_unit", "sat"):
                        with patch.object(settings, "cashu_mints", [PRIMARY_MINT]):
                            return await client.post(
                                "/v1/wallet/topup",
                                params={"cashu_token": "cashuAtest_foreign_token"},
                            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_retries_when_melt_demands_more_than_quoted(
    authenticated_client: AsyncClient,
) -> None:
    """A 179-sat token where every quote reports fee_reserve=1 but the mint
    rejects the first melt demanding 180. The retry shrinks the invoice to 177
    and the topup credits 177 sats (177_000 msats)."""
    mock_token, token_wallet, primary_wallet = _make_swap_mocks(
        179, fee_reserves=[1, 1, 1], mint_url="http://mint.cubabitcoin.org"
    )
    token_wallet.melt.side_effect = [
        Exception(
            "Mint Error: not enough inputs provided for melt. "
            "Provided: 179, needed: 180 (Code: 11000)"
        ),
        Mock(),
    ]

    response = await _post_topup(
        authenticated_client, mock_token, token_wallet, primary_wallet
    )

    assert response.status_code == 200
    assert response.json()["msats"] == 177_000
    assert token_wallet.melt.call_count == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_retries_when_quote_fee_exceeds_estimate(
    authenticated_client: AsyncClient,
) -> None:
    """A 1000-sat token estimated at fee 20, but the real quote demands 23.
    The retry recomputes 1000 - 23 = 977, which fits, and the topup credits
    977 sats (977_000 msats) with a single melt."""
    mock_token, token_wallet, primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[20, 23, 23]
    )

    response = await _post_topup(
        authenticated_client, mock_token, token_wallet, primary_wallet
    )

    assert response.status_code == 200
    assert response.json()["msats"] == 977_000
    assert token_wallet.melt.call_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_topup_returns_400_when_retries_exhausted(
    authenticated_client: AsyncClient,
) -> None:
    """A mint that escalates fee_reserve on every re-quote (1 → 10 → 25 → 50)
    exhausts the retry budget: clean 400 with an actionable message, melt never
    executed."""
    mock_token, token_wallet, primary_wallet = _make_swap_mocks(
        1000, fee_reserves=[1, 10, 25, 50]
    )

    response = await _post_topup(
        authenticated_client, mock_token, token_wallet, primary_wallet
    )

    assert response.status_code == 400
    assert "too small to cover swap fees" in response.json()["detail"]
    assert token_wallet.melt_quote.call_count == 4  # estimation + 3 attempts
    token_wallet.melt.assert_not_called()
