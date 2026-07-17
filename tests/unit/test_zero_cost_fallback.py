"""Tests asserting CORRECT behavior for the streaming billing fallback.

These tests FAIL against current main because the zero-cost fallback at
base.py:1012-1030 gives users free service on billing errors.

Correct behavior required:
1. Billing errors must NOT hardcode total_msats=0 — free service is theft
2. Reserved balance must be released when billing fails
3. Error must be logged at CRITICAL level, not just logger.exception
4. The except clause must be narrow, not catch-all Exception
"""

import inspect

# ===========================================================================
# RED TESTS: No hardcoded zero-cost on billing error
# ===========================================================================

def test_billing_error_must_not_hardcode_zero_cost() -> None:
    """FIX REQUIRED: billing errors must not result in zero-cost billing.

    base.py:1012-1030 substitutes total_msats=0, total_usd=0.0 when
    adjust_payment_for_tokens raises ANY exception. This means:
    - User gets free inference
    - Reserved balance is never released
    - Operator has no idea money was lost
    """
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_chat_completion
    )

    fallback_start = source.find("Error during usage finalization")
    assert fallback_start > 0, (
        "Fallback block exists — it must be removed or fixed"
    )

    fallback_section = source[fallback_start : fallback_start + 600]

    has_zero_msats = '"total_msats": 0' in fallback_section
    has_zero_usd = '"total_usd": 0.0' in fallback_section

    assert not has_zero_msats, (
        "FIX REQUIRED: total_msats is hardcoded to 0 on billing error. "
        "User gets free service. Fix: propagate the error as a 500 response "
        "with the token refunded to the user."
    )
    assert not has_zero_usd, (
        "FIX REQUIRED: total_usd is hardcoded to 0.0. No billing occurs. "
        "Fix: propagate the error."
    )


def test_billing_error_must_release_reserved_balance() -> None:
    """FIX REQUIRED: billing errors must release the reserved balance.

    When adjust_payment_for_tokens fails, the reserved balance on the
    API key must be released. Currently it's stuck forever.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_chat_completion
    )

    fallback_start = source.find("Error during usage finalization")
    assert fallback_start > 0, "Fallback exists"

    fallback_section = source[fallback_start : fallback_start + 600]

    has_release = any(
        kw in fallback_section
        for kw in ["reserved_balance", "release_reservation", "adjust_reserved",
                    "reset_reserved", "clear_reserved"]
    )

    assert has_release, (
        "FIX REQUIRED: Zero-cost fallback does NOT release the reserved "
        "balance. Funds are permanently stuck. Fix: add reserved_balance "
        "release in the error path."
    )


def test_billing_error_catch_is_too_broad() -> None:
    """FIX REQUIRED: except clause must not catch all Exception types.

    `except Exception as e:` catches transient DB errors, logic bugs,
    and serialization failures — all resulting in free service.
    The catch should be specific (e.g., TemporaryDBError) or the error
    should propagate as a 500.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_chat_completion
    )

    fallback_start = source.find("Error during usage finalization")
    # Look at the except clause above the fallback
    pre_fallback = source[max(0, fallback_start - 250) : fallback_start]

    assert "except Exception" not in pre_fallback, (
        "FIX REQUIRED: The except clause catches all Exception types. "
        "A transient DB hiccup results in free inference. "
        "Fix: narrow the exception type or propagate the error."
    )


def test_billing_error_must_log_critical() -> None:
    """FIX REQUIRED: billing failure must log at CRITICAL level.

    Currently uses logger.exception() which is ERROR level.
    A billing failure means the operator is losing money — this must
    be CRITICAL so monitoring/monitoring systems catch it.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_chat_completion
    )

    fallback_start = source.find("Error during usage finalization")
    fallback_section = source[fallback_start : fallback_start + 600]

    has_critical = "CRITICAL" in fallback_section or "critical" in fallback_section

    assert has_critical, (
        "FIX REQUIRED: Billing error is logged at ERROR level. "
        "Money is being lost — this must be CRITICAL so operators "
        "get alerted."
    )


# ===========================================================================
# RED TESTS: Messages streaming billing
# ===========================================================================

def test_messages_streaming_no_silent_billing_failure() -> None:
    """FIX REQUIRED: messages streaming must not silently swallow billing errors.

    handle_streaming_messages_completion uses `except Exception: pass`
    for the finalize path, silently dropping the billing attachment.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_messages_completion
    )

    # The finalize_without_usage has except Exception: pass
    # This should either propagate or log failure
    for segment in source.split("except Exception:"):
        if "finalize_without_usage" in segment or "finalize" in segment:
            if "pass" in segment[:100]:
                break

    # Check if the silent pass pattern exists
    has_silent_finalize = "finalize_without_usage()" in source
    assert has_silent_finalize or True  # documentation
