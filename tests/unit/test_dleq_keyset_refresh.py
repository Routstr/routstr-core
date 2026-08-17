"""Unit tests for keyset-cache refresh on unknown-keyset DLEQ failures."""

from types import SimpleNamespace
from typing import Any

import pytest

from routstr import wallet as wallet_module
from routstr.wallet import _verify_proofs_dleq_with_refresh

UNKNOWN_KEYSET_ERROR = AssertionError(
    "Keyset 01fc0ec0e59cd6fa not known, can not verify DLEQ."
)

TOKEN = SimpleNamespace(mint="https://mint.example.com", unit="sat", amount=100)
PROOFS: list[Any] = []


class FakeWallet:
    """Wallet stub whose DLEQ verification fails until keysets are refreshed."""

    def __init__(
        self,
        fail_first: bool = False,
        fail_always: bool = False,
        error: Exception = UNKNOWN_KEYSET_ERROR,
    ) -> None:
        self.fail_first = fail_first
        self.fail_always = fail_always
        self.error = error
        self.verify_calls = 0
        self.refresh_calls: list[bool] = []

    def verify_proofs_dleq(self, proofs: list[Any]) -> None:
        self.verify_calls += 1
        if self.fail_always or (self.fail_first and not self.refresh_calls):
            raise self.error

    async def load_mint_keysets(self, force_old_keysets: bool = False) -> None:
        self.refresh_calls.append(force_old_keysets)


@pytest.fixture(autouse=True)
def _passthrough_mint_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_mint_operation(operation: Any, **_kwargs: Any) -> Any:
        return await operation()

    monkeypatch.setattr(wallet_module, "run_mint_operation", fake_run_mint_operation)


async def test_no_refresh_when_verification_succeeds() -> None:
    wallet = FakeWallet()
    await _verify_proofs_dleq_with_refresh(wallet, TOKEN, PROOFS)  # type: ignore[arg-type]
    assert wallet.verify_calls == 1
    assert wallet.refresh_calls == []


async def test_unknown_keyset_refreshes_and_retries_once() -> None:
    wallet = FakeWallet(fail_first=True)
    await _verify_proofs_dleq_with_refresh(wallet, TOKEN, PROOFS)  # type: ignore[arg-type]
    assert wallet.verify_calls == 2
    # Refresh must include inactive keysets, which older tokens reference.
    assert wallet.refresh_calls == [True]


async def test_keyset_still_unknown_after_refresh_raises_value_error() -> None:
    wallet = FakeWallet(fail_always=True)
    with pytest.raises(ValueError, match="unknown or ambiguous keyset"):
        await _verify_proofs_dleq_with_refresh(wallet, TOKEN, PROOFS)  # type: ignore[arg-type]
    assert wallet.verify_calls == 2
    assert wallet.refresh_calls == [True]


async def test_invalid_dleq_proof_propagates_without_refresh() -> None:
    # Cashu raises a different message for an actually-invalid DLEQ proof;
    # that must not be masked by a keyset refresh.
    wallet = FakeWallet(fail_always=True, error=AssertionError("DLEQ proof invalid."))
    with pytest.raises(AssertionError, match="DLEQ proof invalid"):
        await _verify_proofs_dleq_with_refresh(wallet, TOKEN, PROOFS)  # type: ignore[arg-type]
    assert wallet.verify_calls == 1
    assert wallet.refresh_calls == []


async def test_refresh_connection_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    wallet = FakeWallet(fail_first=True)

    async def failing_run_mint_operation(operation: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(wallet_module, "run_mint_operation", failing_run_mint_operation)
    with pytest.raises(httpx.ConnectError):
        await _verify_proofs_dleq_with_refresh(wallet, TOKEN, PROOFS)  # type: ignore[arg-type]


async def test_refresh_unclassified_failure_maps_to_mint_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = FakeWallet(fail_first=True)

    async def failing_run_mint_operation(operation: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("keyset db corrupted")

    monkeypatch.setattr(wallet_module, "run_mint_operation", failing_run_mint_operation)
    with pytest.raises(wallet_module.MintConnectionError):
        await _verify_proofs_dleq_with_refresh(wallet, TOKEN, PROOFS)  # type: ignore[arg-type]
