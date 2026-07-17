"""Tests asserting CORRECT behavior for DB persistence and payout safety.

RED tests — FAIL against current main until bugs are fixed.
"""

import inspect

import pytest


# ===========================================================================
# RED TESTS: Fee payout crash safety
# ===========================================================================

def test_fee_payout_pre_reset_or_lock_exists() -> None:
    """FIX REQUIRED: pay-then-reset must become lock-then-pay-then-unlock.

    wallet.py:1076-1080 currently: raw_send_to_lnurl() THEN reset_routstr_fee().
    A crash between these lines causes double payment.

    Fix: set a lock flag BEFORE paying, clear it AFTER resetting.
    On startup, reconcile any locked-but-not-reset payouts.
    """
    from routstr import wallet

    source = inspect.getsource(wallet.periodic_routstr_fee_payout)

    pay_pos = source.find("raw_send_to_lnurl")
    reset_pos = source.find("reset_routstr_fee")

    assert pay_pos > 0 and reset_pos > 0, "Pay and reset both exist"

    # After fix: lock/safeguard must exist BEFORE the pay call
    pre_pay_section = source[:pay_pos]
    has_pre_guard = any(
        kw in pre_pay_section.lower()
        for kw in ["lock", "payout_state", "is_paying", "in_progress",
                    "pre_reset", "reconcile", "checkpoint"]
    )

    assert has_pre_guard, (
        "FIX REQUIRED: Fee payout pays before resetting with no crash guard. "
        "A crash between pay and reset causes double payment. "
        "Fix: add a DB lock/payout_state flag before paying."
    )


# ===========================================================================
# RED TESTS: DB store resilience
# ===========================================================================

def test_retry_wrapper_exists() -> None:
    """FIX REQUIRED: A retry wrapper for critical DB writes must exist."""
    from routstr.core import db

    assert hasattr(db, "store_cashu_transaction_with_retry"), (
        "FIX REQUIRED: No retry wrapper exists for critical money-path "
        "DB writes. Was merged (#600) then reverted (#604). Must be "
        "reinstated with CRITICAL logging on final failure."
    )


# ===========================================================================
# Wallet caching mechanism (informational — not a bug on main)
# ===========================================================================

def test_wallet_cache_uses_global_dict() -> None:
    """get_wallet uses a global _wallets dict — verify mechanism."""
    from routstr import wallet

    source = inspect.getsource(wallet.get_wallet)
    assert "_wallets" in source
    assert "load_mint" in source
    assert "load_proofs" in source


# ===========================================================================
# Mint rate limiter setting (informational)
# ===========================================================================

def test_mint_concurrency_setting_exists_or_documents_gap() -> None:
    """If mint_max_concurrency exists, it must NOT be 0.

    0 disables 429 cooldown tracking on the PR #597 branch.
    """
    from routstr.core.settings import settings

    concurrency = getattr(settings, "mint_max_concurrency", None)
    if concurrency is not None:
        assert concurrency > 0, (
            f"mint_max_concurrency = {concurrency}. 0 disables 429 cooldown."
        )
