"""Regression tests for NUT-02 keyset ID v2 short-id expansion (minibits migration).

minibits migrated to NUT-02 Keyset ID v2: tokens now carry 16-char SHORT
keyset IDs (version byte ``01`` + first 7 bytes of the full 66-char id). The
Cashu mint is agnostic of short ids — the redeeming wallet MUST expand them to
the full id before any melt/swap, otherwise redemption fails with
``A short keyset ID v2 was encountered, but got no keysets to map it to``
(500 "Internal error during token redemption").

These tests pin the behavior of :func:`routstr.wallet._expand_short_keysets`
and verify both redeem paths (same-mint split and cross-mint melt) invoke it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from cashu.core.base import Proof
from cashu.wallet.keyset_manager import KeysetManager

# Matches the live minibits mint: active v2 keyset and its 16-char short id.
FULL_V2_ID = "01fc0ec0e59cd6fa01b7a88f8cd77fce81fd1e64bca67d752e984992b7a3c3a821"
SHORT_V2_ID = "01fc0ec0e59cd6fa"

# Legacy v1 short id (16 hex, version byte 00) == the full id (no expansion).
V1_LEGACY_ID = "00107937db0cc865"


def _keyset(full_id: str) -> SimpleNamespace:
    """Construct a fake keyset object carrying the given full id."""
    return SimpleNamespace(id=full_id)


def _proof(keyset_id: str, amount: int = 1) -> Proof:
    return Proof(
        id=keyset_id,
        amount=amount,
        secret="DEADBEEF",
        C="03" + "00" * 32,
    )


class _ExpandingWallet:
    """A wallet stub whose ``_expand_short_keyset_ids`` is the *real* logic.

    Mirrors the cashu lib's wallet.proofs._expand_short_keyset_ids, so the test
    exercises the actual short->full mapping, not a mock.
    """

    def __init__(
        self,
        keysets: dict[str, SimpleNamespace],
        url: str = "https://mint.example",
        keysets_after_load: dict[str, SimpleNamespace] | None = None,
    ) -> None:
        self.keysets = keysets  # {full_id: obj-with-.id}
        self.url = url
        self.keysets_after_load = keysets_after_load
        self.load_keysets_called = False
        self.expand_called = False

    async def load_mint_keysets(self) -> None:
        self.load_keysets_called = True
        if self.keysets_after_load is not None:
            self.keysets = self.keysets_after_load

    async def _expand_short_keyset_ids(self, proofs: list[Proof]) -> None:
        self.expand_called = True
        manager = KeysetManager()
        keysets_dict = {k.id: k for k in self.keysets.values()}
        for p in proofs:
            if p.id.startswith("01") and len(p.id) == 16:
                p.id = manager.get_full_keyset_id(p.id, keysets_dict)


# ============================================================= helper behavior


@pytest.mark.asyncio
async def test_expand_short_keysets_noop_for_v1_legacy_ids() -> None:
    """v1 (``00``) short ids are the full id — no expansion, no keyset load."""
    from routstr.wallet import _expand_short_keysets

    wallet = _ExpandingWallet({}, url="https://mint.example")
    proofs = [_proof(V1_LEGACY_ID)]
    await _expand_short_keysets(wallet, proofs)
    assert proofs[0].id == V1_LEGACY_ID
    assert wallet.load_keysets_called is False
    assert wallet.expand_called is False


@pytest.mark.asyncio
async def test_expand_short_keysets_noop_for_incomplete_test_proofs() -> None:
    """Proof-like test doubles without string ids are safely ignored."""
    from routstr.wallet import _expand_short_keysets

    wallet = _ExpandingWallet({})
    await _expand_short_keysets(wallet, [Mock(), {"amount": 1}])  # type: ignore[list-item]
    assert wallet.load_keysets_called is False
    assert wallet.expand_called is False


@pytest.mark.asyncio
async def test_expand_short_keysets_noop_for_full_v2_ids() -> None:
    """Already-full 66-char v2 ids are left untouched."""
    from routstr.wallet import _expand_short_keysets

    wallet = _ExpandingWallet({FULL_V2_ID: _keyset(FULL_V2_ID)})
    proofs = [_proof(FULL_V2_ID)]
    await _expand_short_keysets(wallet, proofs)
    assert proofs[0].id == FULL_V2_ID
    assert wallet.load_keysets_called is False


@pytest.mark.asyncio
async def test_expand_short_keysets_expands_v2_short_to_full() -> None:
    """16-char ``01`` short id is expanded to the full 66-char id."""
    from routstr.wallet import _expand_short_keysets

    wallet = _ExpandingWallet(
        {
            FULL_V2_ID: _keyset(FULL_V2_ID),
            V1_LEGACY_ID: _keyset(V1_LEGACY_ID),
        }
    )
    proofs = [_proof(SHORT_V2_ID), _proof(V1_LEGACY_ID)]
    await _expand_short_keysets(wallet, proofs)
    assert proofs[0].id == FULL_V2_ID  # expanded short->full
    assert proofs[1].id == V1_LEGACY_ID  # v1 untouched


@pytest.mark.asyncio
async def test_expand_short_keysets_loads_keysets_when_empty() -> None:
    """When the wallet has no keysets loaded, load them before expansion."""
    from routstr.wallet import _expand_short_keysets

    wallet = _ExpandingWallet(
        {},
        url="https://mint.example",
        keysets_after_load={FULL_V2_ID: _keyset(FULL_V2_ID)},
    )
    proofs = [_proof(SHORT_V2_ID)]
    await _expand_short_keysets(wallet, proofs)
    assert wallet.load_keysets_called is True
    assert proofs[0].id == FULL_V2_ID


@pytest.mark.asyncio
async def test_expand_short_keysets_propagates_keyset_load_failure() -> None:
    """Loading failures retain their original error instead of blaming the token."""
    from routstr.wallet import _expand_short_keysets

    wallet = _ExpandingWallet({})
    load_error = RuntimeError("mint unavailable")
    with (
        patch.object(
            wallet,
            "load_mint_keysets",
            new=AsyncMock(side_effect=load_error),
        ),
        pytest.raises(RuntimeError, match="mint unavailable"),
    ):
        await _expand_short_keysets(wallet, [_proof(SHORT_V2_ID)])


@pytest.mark.asyncio
async def test_expand_short_keysets_refreshes_stale_keysets() -> None:
    """Retry with refreshed keysets when a populated cache cannot map the id."""
    from routstr.wallet import _expand_short_keysets

    stale_id = "01" + "11" * 32
    wallet = _ExpandingWallet(
        {stale_id: _keyset(stale_id)},
        keysets_after_load={FULL_V2_ID: _keyset(FULL_V2_ID)},
    )
    proofs = [_proof(SHORT_V2_ID)]
    await _expand_short_keysets(wallet, proofs)
    assert wallet.load_keysets_called is True
    assert proofs[0].id == FULL_V2_ID


@pytest.mark.asyncio
async def test_expand_short_keysets_wraps_unresolvable_short_id() -> None:
    """A short id that can't be mapped surfaces as a clear ValueError."""
    from routstr.wallet import _expand_short_keysets

    wallet = _ExpandingWallet({FULL_V2_ID: _keyset(FULL_V2_ID)})
    stray = "0111111111111111"  # 16-char short id with no matching keyset
    with pytest.raises(ValueError, match="cannot be mapped"):
        await _expand_short_keysets(wallet, [_proof(stray)])


# ======================================================= redeem paths invoke it


class _PropertyToken:
    """Mimics TokenV4: ``.proofs`` rebuilds fresh Proof objects from scratch
    on every access, so mutating a returned Proof's ``id`` in place is only
    visible to whoever captured that particular list. A caller that re-reads
    ``.proofs`` after expansion would silently get the short id back."""

    def __init__(
        self,
        mint: str,
        unit: str,
        amount: int,
        keysets: list[str],
        keyset_id: str,
        proof_amount: int,
    ) -> None:
        self.mint = mint
        self.unit = unit
        self.amount = amount
        self.keysets = keysets
        self._keyset_id = keyset_id
        self._proof_amount = proof_amount
        self.proofs_access_count = 0

    @property
    def proofs(self) -> list[Proof]:
        self.proofs_access_count += 1
        return [_proof(self._keyset_id, amount=self._proof_amount)]


def _mutate_short_to_full(proofs: list[Proof]) -> None:
    for p in proofs:
        if p.id == SHORT_V2_ID:
            p.id = FULL_V2_ID


@pytest.mark.asyncio
async def test_redeem_same_mint_expands_keysets_before_split() -> None:
    """Same-mint redemption expands short ids and split() sees the full id
    even though token.proofs is a property re-generated on every access."""
    from routstr.wallet import _redeem_same_mint

    token = _PropertyToken(
        mint="https://mint.example",
        unit="sat",
        amount=1,
        keysets=[SHORT_V2_ID],
        keyset_id=SHORT_V2_ID,
        proof_amount=1,
    )

    wallet = AsyncMock()
    wallet.keysets = {FULL_V2_ID: _keyset(FULL_V2_ID)}
    wallet.load_mint = AsyncMock()
    wallet._expand_short_keyset_ids = AsyncMock(side_effect=_mutate_short_to_full)
    wallet.verify_proofs_dleq = AsyncMock()
    wallet.get_fees_for_proofs = Mock(return_value=0)
    wallet.split = AsyncMock(return_value=([], []))
    wallet.url = "https://mint.example"

    with patch("routstr.wallet.run_mint_operation", new=lambda f, **k: f()):
        await _redeem_same_mint(wallet, token)

    split_proofs = wallet.split.call_args.kwargs["proofs"]
    assert split_proofs[0].id == FULL_V2_ID
    assert token.proofs_access_count == 1


@pytest.mark.asyncio
async def test_swap_to_trusted_mint_expands_keysets_before_melt() -> None:
    """Cross-mint swap expands short ids and melt() sees the full id even
    though token.proofs is a property re-generated on every access."""
    import routstr.wallet as wallet_mod

    token = _PropertyToken(
        mint="https://foreign.example",
        unit="sat",
        amount=1000,
        keysets=[SHORT_V2_ID],
        keyset_id=SHORT_V2_ID,
        proof_amount=1000,
    )

    token_wallet = AsyncMock()
    token_wallet.keysets = {FULL_V2_ID: _keyset(FULL_V2_ID)}
    token_wallet.url = "https://foreign.example"
    token_wallet._expand_short_keyset_ids = AsyncMock(side_effect=_mutate_short_to_full)
    token_wallet.melt_quote = AsyncMock(
        return_value=Mock(quote="melt-q", amount=1000, fee_reserve=0)
    )
    token_wallet.melt = AsyncMock(return_value=Mock(state="PAID"))
    token_wallet.get_fees_for_proofs = Mock(return_value=0)

    dest_wallet = AsyncMock()
    dest_wallet.available_balance.amount = 0

    with (
        patch.object(
            wallet_mod,
            "_calculate_swap_amount",
            new=AsyncMock(return_value=1000),
        ),
        patch.object(
            wallet_mod,
            "_request_mint_with_fallback",
            new=AsyncMock(
                return_value=(dest_wallet, "https://mint.example", Mock(quote="q"))
            ),
        ),
        patch.object(
            wallet_mod,
            "_confirm_melt_paid",
            new=AsyncMock(),
        ),
        patch("routstr.wallet.run_mint_operation", new=lambda f, **k: f()),
    ):
        with patch.object(
            wallet_mod,
            "_trusted_destination_candidates",
            return_value=["https://mint.example"],
        ):
            await wallet_mod.swap_to_trusted_mint(
                token, token_wallet, destination_mints=["https://mint.example"]
            )

    melt_proofs = token_wallet.melt.call_args.kwargs["proofs"]
    assert melt_proofs[0].id == FULL_V2_ID
    assert token.proofs_access_count == 1
