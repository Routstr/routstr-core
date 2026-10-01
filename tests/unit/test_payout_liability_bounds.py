"""Owner payout keeps each wallet's declared liability and the global total.

Regression for multi-mint payout starvation: subtracting the *total* user
liability from every wallet hid the owner surplus on any mint holding less
than the whole liability, so only the largest wallet could ever pay out.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest

from routstr.core.settings import settings
from routstr.wallet import _owner_balance_for_mint_and_unit, _payout_mint_and_unit

MINT_A = "https://a.test"
MINT_B = "https://b.test"


@asynccontextmanager
async def _session() -> AsyncIterator[Mock]:
    yield Mock()


def _keyset_id(mint_url: str, unit: str) -> str:
    return f"{mint_url}|{unit}"


@contextmanager
def _wallet_db(
    sat_proofs: dict[str, int], reserved: frozenset[str] = frozenset()
) -> Iterator[AsyncMock]:
    """One sat keyset per mint, one proof behind it. Yields the get_wallet mock."""
    keysets = [
        Mock(id=_keyset_id(mint_url, "sat"), mint_url=mint_url, unit="sat")
        for mint_url in sat_proofs
    ]
    proofs = [
        Mock(
            id=_keyset_id(mint_url, "sat"),
            amount=amount,
            reserved=mint_url in reserved,
        )
        for mint_url, amount in sat_proofs.items()
    ]
    get_wallet = AsyncMock(return_value=Mock(url=MINT_B, db=Mock()))
    with (
        patch("routstr.wallet.get_wallet", get_wallet),
        patch("routstr.wallet.get_cashu_keysets", AsyncMock(return_value=keysets)),
        patch("routstr.wallet.get_cashu_proofs", AsyncMock(return_value=proofs)),
    ):
        yield get_wallet


@contextmanager
def _env() -> Iterator[None]:
    with (
        patch("routstr.wallet.db.create_session", _session),
        patch.object(settings, "cashu_mints", [MINT_A, MINT_B]),
        patch.object(settings, "primary_mint", MINT_A),
    ):
        yield


@contextmanager
def _liabilities(per_mint_sats: dict[str, int], total_sats: int) -> Iterator[None]:
    async def per_mint(_session: object, mint_url: str, unit: str) -> int:
        return per_mint_sats.get(mint_url, 0) * 1000

    with (
        _env(),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(side_effect=per_mint),
        ),
        patch(
            "routstr.wallet.db.total_user_liability",
            AsyncMock(return_value=total_sats * 1000),
        ),
    ):
        yield


@pytest.mark.asyncio
async def test_owner_balance_keeps_only_the_wallets_own_liability() -> None:
    with (
        _liabilities({MINT_A: 216, MINT_B: 34}, total_sats=250),
        _wallet_db({MINT_A: 400, MINT_B: 270}),
    ):
        assert await _owner_balance_for_mint_and_unit(MINT_B, "sat", 270) == 236
        assert await _owner_balance_for_mint_and_unit(MINT_A, "sat", 400) == 184


@pytest.mark.asyncio
async def test_owner_balance_never_exceeds_global_surplus() -> None:
    """Liability nobody declared against a mint is still covered in aggregate."""
    with (
        _liabilities({}, total_sats=250),
        _wallet_db({MINT_A: 100, MINT_B: 270}),
    ):
        assert await _owner_balance_for_mint_and_unit(MINT_B, "sat", 270) == 120


@pytest.mark.asyncio
async def test_proofs_of_an_untrusted_mint_do_not_raise_the_bound() -> None:
    """Only configured mints back the global surplus."""
    with (
        _liabilities({MINT_A: 216, MINT_B: 34}, total_sats=250),
        patch.object(settings, "cashu_mints", [MINT_B]),
        patch.object(settings, "primary_mint", MINT_B),
        _wallet_db({MINT_A: 400, MINT_B: 270}),
    ):
        assert await _owner_balance_for_mint_and_unit(MINT_B, "sat", 270) == 20


@pytest.mark.asyncio
async def test_reserved_proofs_do_not_raise_the_bound() -> None:
    """Another process may already be spending them."""
    with (
        _liabilities({MINT_A: 216, MINT_B: 34}, total_sats=250),
        _wallet_db({MINT_A: 400, MINT_B: 270}, reserved=frozenset({MINT_A})),
    ):
        assert await _owner_balance_for_mint_and_unit(MINT_B, "sat", 270) == 20


@pytest.mark.asyncio
async def test_duplicate_configured_mint_is_counted_once() -> None:
    """A mint listed twice in CASHU_MINTS would otherwise raise the global bound."""
    with (
        _liabilities({}, total_sats=600),
        patch.object(settings, "cashu_mints", [MINT_A, MINT_A, MINT_B]),
        _wallet_db({MINT_A: 400, MINT_B: 270}),
    ):
        assert await _owner_balance_for_mint_and_unit(MINT_B, "sat", 270) == 70


@pytest.mark.asyncio
async def test_cross_wallet_bound_asks_no_mint_for_metadata() -> None:
    """The sum is local. Loading a wallet per mint and unit rate-limited mints."""
    with (
        _liabilities({MINT_A: 216, MINT_B: 34}, total_sats=250),
        _wallet_db({MINT_A: 400, MINT_B: 270}) as get_wallet,
    ):
        await _owner_balance_for_mint_and_unit(MINT_B, "sat", 270)
    assert get_wallet.await_args_list
    assert all(c.kwargs.get("load") is False for c in get_wallet.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mint_liability,total_liability,expected",
    [(1_500, 2_200, 1_800), (2_700, 1_500, 1_300)],
)
async def test_msat_wallet_surplus_is_not_rounded(
    mint_liability: int, total_liability: int, expected: int
) -> None:
    """Either bound can bind, and neither is rounded to whole sats."""
    with (
        _env(),
        _wallet_db({}),
        patch(
            "routstr.wallet.db.user_liability_for_mint_and_unit",
            AsyncMock(return_value=mint_liability),
        ),
        patch(
            "routstr.wallet.db.total_user_liability",
            AsyncMock(return_value=total_liability),
        ),
    ):
        assert await _owner_balance_for_mint_and_unit(MINT_B, "msat", 4_000) == expected


@pytest.mark.asyncio
async def test_payout_sends_the_smaller_wallets_surplus() -> None:
    send = AsyncMock(return_value=236_000)
    with (
        _liabilities({MINT_A: 216, MINT_B: 34}, total_sats=250),
        patch.object(settings, "min_payout_sat", 50),
        patch.object(settings, "max_payout_sat", 250_000),
        _wallet_db({MINT_A: 400, MINT_B: 270}),
        patch(
            "routstr.wallet.get_proofs_per_mint_and_unit",
            Mock(return_value=[Mock(amount=270)]),
        ),
        patch(
            "routstr.wallet.slow_filter_spend_proofs",
            AsyncMock(side_effect=lambda proofs, wallet: proofs),
        ),
        patch("routstr.wallet.asyncio.sleep", AsyncMock()),
        patch("routstr.wallet.raw_send_to_lnurl", send),
    ):
        await _payout_mint_and_unit(MINT_B, "sat")
    assert send.await_args is not None
    assert send.await_args.kwargs["amount"] == 236
