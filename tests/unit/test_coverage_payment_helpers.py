"""Coverage-filling tests for payment/helpers.py (currently 52% coverage).

Tests the real public API: check_token_balance, get_max_cost_for_model,
estimate_tokens, create_error_response, etc.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest


# ---------------------------------------------------------------------------
# check_token_balance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_token_balance_x_cashu_present() -> None:
    """X-Cashu header triggers token deserialization and balance check."""
    from routstr.payment.helpers import check_token_balance

    headers = {"x-cashu": "cashuAtest_token"}
    body = {"model": "gpt-4"}

    with patch("routstr.payment.helpers.deserialize_token_from_string") as mock_deser:
        mock_token = Mock()
        mock_token.amount = 50000
        mock_token.unit = "sat"
        mock_deser.return_value = mock_token

        # Should not raise — balance is sufficient
        check_token_balance(headers, body, 1000)


@pytest.mark.asyncio
async def test_check_token_balance_no_x_cashu_raises() -> None:
    """Missing X-Cashu header raises HTTPException (401 on main)."""
    from fastapi import HTTPException

    from routstr.payment.helpers import check_token_balance

    headers = {}
    body = {"model": "gpt-4"}

    with pytest.raises(HTTPException) as exc_info:
        check_token_balance(headers, body, 1000)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_check_token_balance_insufficient_raises() -> None:
    """Token with insufficient balance raises HTTPException 402.

    max_cost_for_model is in msat, so with amount=100 sat (=100,000 msat),
    max_cost=200,000 msat triggers the insufficient balance check.
    """
    from fastapi import HTTPException

    from routstr.payment.helpers import check_token_balance

    headers = {"x-cashu": "cashuAtest_token"}
    body = {"model": "gpt-4"}

    with patch("routstr.payment.helpers.deserialize_token_from_string") as mock_deser:
        mock_token = Mock()
        mock_token.amount = 100  # 100 sat
        mock_token.unit = "sat"
        mock_deser.return_value = mock_token

        with pytest.raises(HTTPException) as exc_info:
            # 200,000 msat > 100,000 msat (100 sat * 1000)
            check_token_balance(headers, body, 200000)

        assert exc_info.value.status_code == 402


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

def test_estimate_tokens_empty_messages() -> None:
    """Empty message list returns 0 tokens."""
    from routstr.payment.helpers import estimate_tokens

    result = estimate_tokens([])

    assert result == 0


def test_estimate_tokens_text_content() -> None:
    """Text messages are counted."""
    from routstr.payment.helpers import estimate_tokens

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello, how are you?"},
    ]

    result = estimate_tokens(messages)

    assert result > 0
    assert isinstance(result, int)


def test_estimate_tokens_long_text() -> None:
    """Longer messages produce higher token counts."""
    from routstr.payment.helpers import estimate_tokens

    short = estimate_tokens([{"role": "user", "content": "Hi"}])
    long = estimate_tokens([{"role": "user", "content": "Hello " * 100}])

    assert long > short


# ---------------------------------------------------------------------------
# create_error_response
# ---------------------------------------------------------------------------

def test_create_error_response_402() -> None:
    """402 Payment Required error is properly formatted."""
    from fastapi import Request

    from routstr.payment.helpers import create_error_response

    request = Request(scope={"type": "http", "method": "GET"})
    result = create_error_response("insufficient_funds", "Insufficient balance", 402, request)

    assert result.status_code == 402


def test_create_error_response_500() -> None:
    """500 Internal Server Error is properly formatted."""
    from fastapi import Request

    from routstr.payment.helpers import create_error_response

    request = Request(scope={"type": "http", "method": "GET"})
    result = create_error_response("server_error", "Internal error", 500, request)

    assert result.status_code == 500


# ---------------------------------------------------------------------------
# Image token estimation helpers
# ---------------------------------------------------------------------------

def test_image_dimensions_valid_png() -> None:
    """_get_image_dimensions returns width and height for a valid PNG."""
    from routstr.payment.helpers import _get_image_dimensions

    # A minimal 1x1 red PNG (valid minimal file)
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
        b"\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\x0f\x00\x00\x01\x01\x00\x05"
        b"\x18\xd8N"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    w, h = _get_image_dimensions(png)
    assert w == 1
    assert h == 1


def test_calculate_image_tokens_low_detail() -> None:
    """Low detail images are always 85 tokens."""
    from routstr.payment.helpers import _calculate_image_tokens

    tokens = _calculate_image_tokens(1024, 1024, "low")

    assert tokens == 85


def test_calculate_image_tokens_high_detail() -> None:
    """High detail images are scaled and tile-based."""
    from routstr.payment.helpers import _calculate_image_tokens

    tokens = _calculate_image_tokens(1024, 1024, "high")

    assert tokens > 85
    assert isinstance(tokens, int)
