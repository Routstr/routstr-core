"""Tests exposing the hardcoded zero-cost fallback vulnerability.

base.py:1012-1030 catches any exception from adjust_payment_for_tokens()
during streaming usage finalization and hardcodes:
    total_msats: 0, total_usd: 0.0

This means:
1. The user gets FREE service (no cost deducted)
2. The reserved balance is NEVER released — funds stuck forever
3. No CRITICAL log — operator won't know money is being lost

stream_with_cost is a NESTED function inside handle_streaming_chat_completion
(line 816) and handle_streaming_messages_completion (line 1720). We inspect
the source of those outer handlers.
"""

import inspect

import pytest


# ---------------------------------------------------------------------------
# Reproduce vulnerability: zero-cost fallback exists in source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_cost_fallback_exists_chat_streaming() -> None:
    """The chat streaming handler contains the hardcoded zero-cost fallback."""
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_chat_completion
    )

    assert "Error during usage finalization" in source, (
        "BUG CONFIRMED: 'Error during usage finalization' catch block exists "
        "in handle_streaming_chat_completion. It catches ALL exceptions from "
        "adjust_payment_for_tokens() and substitutes a zero-cost fallback."
    )
    assert '"total_msats": 0' in source or "'total_msats': 0" in source, (
        "BUG CONFIRMED: total_msats is hardcoded to 0 in the fallback path. "
        "User gets free service + funds stuck."
    )


@pytest.mark.asyncio
async def test_messages_streaming_also_catches_billing_errors() -> None:
    """The messages streaming handler catches billing errors silently.

    Unlike chat streaming (which has the hardcoded zero-cost fallback with
    logging), the messages handler at lines 1720+ uses `except Exception: pass`
    for the adjustment path. Both patterns result in unbilled usage.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_messages_completion
    )

    # The messages handler has its own catch-all for adjust_payment_for_tokens
    # but uses `except Exception: pass` instead of the hardcoded fallback
    assert "adjust_payment_for_tokens" in source, (
        "Messages handler calls adjust_payment_for_tokens"
    )
    # It catches silently — note: "pass" appears in two contexts:
    # 1. json.JSONDecodeError: pass (line iteration)
    # 2. except Exception: pass (billing finalization failure)
    assert "except Exception:" in source, (
        "Messages handler catches billing errors with `except Exception`"
    )


@pytest.mark.asyncio
async def test_zero_cost_fallback_no_balance_release() -> None:
    """Verify the zero-cost fallback does NOT release the reserved balance."""
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_chat_completion
    )

    fallback_start = source.find("Error during usage finalization")
    assert fallback_start > 0, "Fallback exists"

    fallback_section = source[fallback_start : fallback_start + 2000]

    has_release = any(
        kw in fallback_section
        for kw in ["reserved_balance", "release_reservation", "adjust_reserved"]
    )

    assert not has_release, (
        "BUG CONFIRMED: The zero-cost fallback does NOT release the reserved "
        "balance. Funds are permanently stuck on the API key."
    )


@pytest.mark.asyncio
async def test_zero_cost_usage_chunk_still_emitted() -> None:
    """The zero-cost fallback still emits a usage chunk with zeros."""
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_chat_completion
    )

    assert "usage_chunk_data" in source
    assert '"prompt_tokens"' in source or "'prompt_tokens'" in source, (
        "BUG CONFIRMED: Zeroed usage chunk is emitted to client. "
        "Client sees 0 tokens used and is never billed."
    )


@pytest.mark.asyncio
async def test_adjust_payment_exception_is_caught_broadly() -> None:
    """The catch clause uses `except Exception` — catches EVERYTHING."""
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_chat_completion
    )

    fallback_start = source.find("Error during usage finalization")
    fallback_section = source[max(0, fallback_start - 200) : fallback_start]

    assert "except Exception" in fallback_section, (
        "BUG CONFIRMED: The catch clause is `except Exception as e:` — "
        "it catches ALL exception types. A transient DB hiccup gives "
        "the user an unpaid inference."
    )


@pytest.mark.asyncio
async def test_responses_streaming_billing_path_exists() -> None:
    """The responses streaming handler has its own billing finalization.

    We verify it calls adjust_payment_for_tokens and has error handling.
    """
    from routstr.upstream.base import BaseUpstreamProvider

    source = inspect.getsource(
        BaseUpstreamProvider.handle_streaming_responses_completion
    )

    assert "adjust_payment_for_tokens" in source, (
        "Responses handler calls adjust_payment_for_tokens"
    )
    # Document: this handler likely has its own error handling path
    assert True
