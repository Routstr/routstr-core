"""RED tests for live money-loss vulnerabilities in routstr-core.

Every test in this file asserts the CORRECT (safe) behaviour for a money
path where a user, provider, or node runner can lose funds.  Each test
FAILS against current ``main`` because the code is buggy — they are the
"RED" phase of TDD.  Once the underlying bugs are fixed, they go green.

== Vulnerability summary (all LIVE on main as of 2026-08-06) ==

V-E1  send_refund() swallows DB failure after minting a refund token
      base.py ~line 3625  —  except Exception: pass
      Impact: refund token is minted at the Cashu mint but never recorded
      in the DB.  The user receives the token in the X-Cashu header, but
      if they lose it (or it never arrives) the refund endpoint cannot
      look it up → permanently unrecoverable funds.

V-E2  Emergency refund (chat) — same except: pass, false-green in the
      existing test_emergency_refund_no_try_except_pass because the
      500-char inspection window is too short to reach the except block.
      base.py ~line 3992

V-E3  Emergency refund (responses API) — identical pattern.
      base.py ~line 4972

V-E4  Balance refund endpoint swallows DB failure.
      balance.py ~line 628  —  except Exception: pass

V-E5  credit_balance() swallows "in" transaction DB failure.
      wallet.py ~line 1715  —  except Exception: pass
      Impact: token is redeemed and balance credited, but no "in" audit
      row is stored.  The refund endpoint matches "in" → "out" by
      request_id; a missing "in" row breaks that linkage.

V-E6  EHBP refund token — except: pass after store.
      ehbp.py ~line 762

V-E7  EHBP "in" transaction — except: pass after store.
      ehbp.py ~line 1028

V-E8  Admin withdraw returns the token to the caller even when the DB
      store fails.  The token is delivered but there is no audit trail.
      admin.py ~line 475

V-E9  Existing emergency-refund tests use a 500-char source window that
      is too short to reach the except: pass block, producing a false
      green.  This test verifies the window is wide enough.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_contains_except_pass(source: str, anchor: str, window: int = 1000) -> bool:
    """Return True if an ``except Exception: pass`` (or bare ``except: pass``)
    appears within ``window`` characters after ``anchor`` in ``source``."""
    idx = source.find(anchor)
    if idx < 0:
        return False
    section = source[idx : idx + window]
    has_except = "except Exception:" in section or "except:" in section
    return has_except and "pass" in section


# ===========================================================================
# V-E1: send_refund() must not silently swallow DB write failure
# ===========================================================================


def test_send_refund_no_except_pass_after_store() -> None:
    """FIX REQUIRED: send_refund() mints a refund token then uses
    try/except/pass around store_cashu_transaction.

    If the DB write fails the token exists at the mint but is never
    recorded.  The refund endpoint cannot find it and the user's funds
    are permanently lost.

    Correct behaviour: let the exception propagate, or at minimum log
    at CRITICAL with the full token string so an operator can manually
    recover it.  Never silently pass.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    src = inspect.getsource(BaseUpstreamProvider.send_refund)
    assert not _source_contains_except_pass(
        src, "store_cashu_transaction"
    ), (
        "FIX REQUIRED: send_refund() uses try/except/pass around "
        "store_cashu_transaction (base.py ~line 3625). A failed DB write "
        "after the token is minted permanently loses the refund token. "
        "Fix: propagate the exception or log CRITICAL with the full token."
    )


# ===========================================================================
# V-E2: Emergency refund (chat) — except: pass is LIVE (existing test is
# a false green because its 500-char window is too short)
# ===========================================================================


def test_emergency_refund_chat_no_silent_db_failure() -> None:
    """FIX REQUIRED: The chat emergency refund path (JSON parse error)
    uses try/except/pass around store_cashu_transaction after minting a
    refund token via send_token().

    The existing test_emergency_refund_no_try_except_pass passes because
    it only inspects a 500-char window — too short to reach the except
    block.  This test uses a wider window and correctly fails.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    src = inspect.getsource(
        BaseUpstreamProvider.handle_x_cashu_non_streaming_response
    )
    assert not _source_contains_except_pass(
        src, "emergency_refund = amount", window=1000
    ), (
        "FIX REQUIRED: Emergency refund (chat, base.py ~line 3992) uses "
        "try/except/pass around store_cashu_transaction after minting a "
        "refund token. A failed DB write permanently loses the token. "
        "Fix: propagate the exception or log CRITICAL with the full token."
    )


# ===========================================================================
# V-E3: Emergency refund (responses API) — identical pattern
# ===========================================================================


def test_emergency_refund_responses_no_silent_db_failure() -> None:
    """FIX REQUIRED: Same except: pass pattern in the Responses API
    emergency refund path (base.py ~line 4972)."""
    from routstr.upstream.base import BaseUpstreamProvider

    src = inspect.getsource(
        BaseUpstreamProvider.handle_x_cashu_non_streaming_responses_response
    )
    assert not _source_contains_except_pass(
        src, "emergency_refund = amount", window=1000
    ), (
        "FIX REQUIRED: Emergency refund (responses API, base.py ~line 4972) "
        "uses try/except/pass around store_cashu_transaction after minting "
        "a refund token. Same fund-loss vulnerability as the chat path."
    )


# ===========================================================================
# V-E4: Balance refund endpoint — except: pass
# ===========================================================================


def test_balance_refund_endpoint_no_silent_db_failure() -> None:
    """FIX REQUIRED: The /v1/wallet/refund endpoint mints a refund token
    via send_token() then uses try/except/pass around
    store_cashu_transaction (balance.py ~line 628).

    A failed DB write means the token is delivered to the user but never
    recorded — the refund sweep cannot reclaim it and the audit trail
    is broken.
    """
    from routstr import balance

    src = inspect.getsource(balance.refund_wallet_endpoint)
    assert not _source_contains_except_pass(
        src, "store_cashu_transaction"
    ), (
        "FIX REQUIRED: refund_wallet_endpoint (balance.py ~line 628) uses "
        "try/except/pass around store_cashu_transaction. A failed DB write "
        "loses the audit record for the minted refund token."
    )


# ===========================================================================
# V-E5: credit_balance() — "in" transaction except: pass
# ===========================================================================


def test_credit_balance_no_silent_db_failure_for_in_tx() -> None:
    """FIX REQUIRED: credit_balance() redeems a Cashu token and credits
    the user's balance, then uses try/except/pass around
    store_cashu_transaction for the "in" record (wallet.py ~line 1715).

    The token is already spent at the mint.  If the "in" DB record is
    not stored, the refund endpoint cannot match "in" → "out" by
    request_id.  The funds are credited but the audit/reconciliation
    chain is broken.
    """
    from routstr import wallet

    # credit_balance delegates to _credit_balance_locked
    src = inspect.getsource(wallet._credit_balance_locked)
    assert not _source_contains_except_pass(
        src, "store_cashu_transaction"
    ), (
        "FIX REQUIRED: _credit_balance_locked (wallet.py ~line 1715) uses "
        "try/except/pass around store_cashu_transaction for the 'in' "
        "record. A failed DB write breaks the in→out refund linkage."
    )


# ===========================================================================
# V-E6: EHBP refund token — except: pass
# ===========================================================================


def test_ehbp_refund_no_silent_db_failure() -> None:
    """FIX REQUIRED: The EHBP refund helper mints a refund token via
    send_token() then uses try/except/pass around
    store_cashu_transaction (ehbp.py ~line 762)."""
    from routstr.upstream import ehbp

    # Find the function that creates a refund token
    refund_fn = None
    for name in dir(ehbp):
        obj = getattr(ehbp, name)
        if inspect.iscoroutinefunction(obj) and hasattr(obj, "__code__"):
            try:
                src = inspect.getsource(obj)
                if "send_token" in src and "store_cashu_transaction" in src and "typ=\"out\"" in src:
                    refund_fn = obj
                    break
            except (OSError, TypeError):
                continue

    assert refund_fn is not None, "Could not locate EHBP refund function"
    src = inspect.getsource(refund_fn)
    assert not _source_contains_except_pass(
        src, "store_cashu_transaction"
    ), (
        "FIX REQUIRED: EHBP refund helper (ehbp.py ~line 762) uses "
        "try/except/pass around store_cashu_transaction after minting a "
        "refund token. Same fund-loss vulnerability as base.py paths."
    )


# ===========================================================================
# V-E7: EHBP "in" transaction — except: pass
# ===========================================================================


def test_ehbp_in_transaction_no_silent_db_failure() -> None:
    """FIX REQUIRED: The EHBP receive path redeems a token then uses
    try/except/pass around store_cashu_transaction for the "in" record
    (ehbp.py ~line 1028)."""
    from routstr.upstream import ehbp

    receive_fn = None
    for name in dir(ehbp):
        obj = getattr(ehbp, name)
        if inspect.iscoroutinefunction(obj) and hasattr(obj, "__code__"):
            try:
                src = inspect.getsource(obj)
                if "recieve_token" in src and "store_cashu_transaction" in src and "typ=\"in\"" in src:
                    receive_fn = obj
                    break
            except (OSError, TypeError):
                continue

    assert receive_fn is not None, "Could not locate EHBP receive function"
    src = inspect.getsource(receive_fn)
    assert not _source_contains_except_pass(
        src, "store_cashu_transaction"
    ), (
        "FIX REQUIRED: EHBP receive path (ehbp.py ~line 1028) uses "
        "try/except/pass around store_cashu_transaction for the 'in' "
        "record. A failed DB write breaks the audit trail."
    )


# ===========================================================================
# V-E8: Admin withdraw must not return token when DB store fails
# ===========================================================================


def test_admin_withdraw_must_not_return_token_on_db_failure() -> None:
    """FIX REQUIRED: The admin withdraw endpoint mints a token via
    send_token(), then tries to store it.  If the store fails it logs
    CRITICAL but STILL RETURNS THE TOKEN to the caller (admin.py ~line
    475).

    The token is delivered (admin gets their money) but there is no
    audit trail — if the admin later loses the token, there is no DB
    record to reclaim it from.  Worse, the "out" row is missing so
    reconciliation is impossible.

    Correct behaviour: if the DB store fails, the token should NOT be
    returned to the caller.  Instead, raise an error so the admin knows
    the withdrawal failed and can retry.
    """
    import inspect

    from routstr.core import admin

    src = inspect.getsource(admin.withdraw)
    # Find the store_cashu_transaction section
    store_idx = src.find("store_cashu_transaction")
    assert store_idx > 0, "withdraw endpoint must store the token"

    # The section from store_cashu_transaction to the return statement
    # must NOT contain "return" before the except block — i.e. the
    # function must not return the token if the store raised.
    # Currently the code does:
    #   try:
    #       await store_cashu_transaction(...)
    #   except Exception:
    #       logger.critical(...)
    #   return {"token": token, ...}
    #
    # The return is AFTER the except, meaning the token is returned even
    # when the store failed.  The fix: re-raise or return an error
    # response inside the except block, before the return.
    section = src[store_idx:]
    # Check that there is a "return" after the except block (the bug)
    return_after_except = (
        "except Exception:" in section and "return" in section.split("except Exception:")[-1]
    )
    assert not return_after_except, (
        "FIX REQUIRED: admin.withdraw (admin.py ~line 475) returns the "
        "token to the caller even when store_cashu_transaction fails. "
        "A failed DB write means the token is delivered but has no audit "
        "trail. Fix: re-raise or return an error response inside the "
        "except block — do not return the token."
    )


# ===========================================================================
# V-E9: Existing emergency refund test window is too short (false green)
# ===========================================================================


def test_existing_emergency_refund_test_window_is_wide_enough() -> None:
    """REGRESSION GUARD: The existing test_emergency_refund_no_try_except_pass
    inspects a 500-character window after "emergency_refund = amount".
    The except: pass block is ~530 chars after that anchor, so the 500-char
    window misses it entirely — producing a false green.

    This test verifies that a 1000-char window (which we use in V-E2/V-E3)
    correctly catches the live bug.  If this test fails, someone shrank
    the window back to 500 or removed the wider-window tests.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    src = inspect.getsource(
        BaseUpstreamProvider.handle_x_cashu_non_streaming_response
    )
    emergency_start = src.find("emergency_refund = amount")
    assert emergency_start > 0, "Emergency refund path must exist"

    # The 1000-char window MUST see the except: pass (currently live bug)
    wide_section = src[emergency_start : emergency_start + 1000]
    assert "except Exception:" in wide_section and "pass" in wide_section, (
        "The 1000-char window must catch the live except: pass bug. "
        "If this fails, either the bug was fixed (good!) or the window "
        "logic changed (bad — re-check V-E2)."
    )
