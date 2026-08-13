"""Compatibility shims for incoming Cashu token proofs.

Ecosystem migrations (NUT spec revisions, wallet-specific quirks) periodically
change the shape of tokens we receive. Each such fix is a
:class:`ProofCompatShim`; :func:`normalize_token_proofs` is the single entry
point the redeem/swap paths call before touching a token's proofs.

To add a new shim: subclass :class:`ProofCompatShim` and append an instance to
:data:`PROOF_SHIMS`.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from cashu.core.base import Proof, Token

from .mint import run_mint_operation

if TYPE_CHECKING:
    from cashu.wallet.wallet import Wallet as _CashuWallet


def is_short_v2_keyset_id(keyset_id: object) -> bool:
    """True for a 16-char NUT-02 v2 short keyset id (version byte ``01``).

    Short ids exist only inside tokens; mint keysets are keyed by the full
    66-char id, so a short id can never be activated or looked up directly.
    """
    return (
        isinstance(keyset_id, str)
        and keyset_id.startswith("01")
        and len(keyset_id) == 16
    )


class ProofCompatShim(ABC):
    """One token-format compatibility fix applied to proofs in place."""

    @abstractmethod
    def applies(self, proofs: list[Proof]) -> bool:
        """Cheap check whether this shim is needed for these proofs."""

    @abstractmethod
    async def apply(self, wallet: "_CashuWallet", proofs: list[Proof]) -> None:
        """Mutate ``proofs`` in place; may talk to the mint via ``wallet``."""


class ShortKeysetIdExpansion(ProofCompatShim):
    """Expand short NUT-02 v2 keyset ids (e.g. minibits tokens) to full
    66-char ids in place; the mint won't accept a short id on melt/swap."""

    def applies(self, proofs: list[Proof]) -> bool:
        return any(
            is_short_v2_keyset_id(getattr(proof, "id", None)) for proof in proofs
        )

    async def apply(self, wallet: "_CashuWallet", proofs: list[Proof]) -> None:
        had_keysets = bool(wallet.keysets)

        async def load_keysets() -> None:
            await run_mint_operation(
                lambda: wallet.load_mint_keysets(),
                op_name="load_mint_for_keyset_expansion",
                mint_url=wallet.url,
                retry_timeouts=False,
            )

        if not had_keysets:
            await load_keysets()
        try:
            try:
                await wallet._expand_short_keyset_ids(proofs)
            except KeyError:
                if not had_keysets:
                    raise
                # Cached keysets may predate the token's issuing keyset.
                await load_keysets()
                await wallet._expand_short_keyset_ids(proofs)
        except KeyError as e:
            raise ValueError(
                "Token carries a short keyset id that cannot be mapped to a "
                f"mint keyset (NUT-02 v2 migration): {e}"
            ) from e


PROOF_SHIMS: tuple[ProofCompatShim, ...] = (ShortKeysetIdExpansion(),)


async def normalize_token_proofs(
    wallet: "_CashuWallet", token_obj: Token
) -> list[Proof]:
    """Capture a token's proofs once and run every applicable compat shim.

    ``token_obj.proofs`` rebuilds fresh Proof objects on every access, so
    callers must use the returned list — re-reading ``token_obj.proofs`` after
    this call silently discards the shims' in-place mutations.
    """
    proofs = token_obj.proofs
    for shim in PROOF_SHIMS:
        if shim.applies(proofs):
            await shim.apply(wallet, proofs)
    return proofs
