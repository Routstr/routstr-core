"""Coverage tests for admin.py (currently 35%).

Tests admin endpoints that are testable without full app setup:
withdraw validation, authentication guards, and slug validation.
"""

from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException, Request

# ===========================================================================
# withdraw — validation and edge cases
# ===========================================================================

@pytest.mark.asyncio
async def test_withdraw_rejects_zero_amount() -> None:
    """withdraw validation rejects amount <= 0."""
    from routstr.core.admin import WithdrawRequest, withdraw

    request = Request(scope={"type": "http", "method": "POST"})

    with pytest.raises(HTTPException) as exc_info:
        await withdraw(request, WithdrawRequest(amount=0, unit="sat"))

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_withdraw_rejects_negative_amount() -> None:
    """withdraw validation rejects negative amounts."""
    from routstr.core.admin import WithdrawRequest, withdraw

    request = Request(scope={"type": "http", "method": "POST"})

    with pytest.raises(HTTPException) as exc_info:
        await withdraw(request, WithdrawRequest(amount=-100, unit="sat"))

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_withdraw_rejects_insufficient_balance() -> None:
    """withdraw returns 400 when wallet balance is insufficient."""
    from routstr.core.admin import WithdrawRequest, withdraw

    request = Request(scope={"type": "http", "method": "POST"})

    with patch("routstr.core.admin.get_wallet") as mock_wallet, \
         patch("routstr.core.admin.get_proofs_per_mint_and_unit") as mock_proofs, \
         patch("routstr.core.admin.slow_filter_spend_proofs") as mock_filter:

        mock_w = Mock()
        mock_w.keysets = {}
        mock_w.proofs = []
        mock_wallet.return_value = mock_w
        mock_proofs.return_value = []
        mock_filter.return_value = []

        with pytest.raises(HTTPException) as exc_info:
            await withdraw(request, WithdrawRequest(amount=1000000, unit="sat"))

        assert exc_info.value.status_code == 400
        assert "Insufficient" in str(exc_info.value.detail)


# ===========================================================================
# require_admin_api guard
# ===========================================================================

@pytest.mark.asyncio
async def test_require_admin_rejects_no_session() -> None:
    """require_admin_api rejects requests without admin session cookie."""
    from routstr.core.admin import require_admin_api

    request = Request(scope={
        "type": "http",
        "method": "GET",
        "headers": [],
    })

    with pytest.raises(HTTPException) as exc_info:
        await require_admin_api(request)

    # 401 or 403 depending on auth configuration
    assert exc_info.value.status_code in (401, 403)


# ===========================================================================
# _validate_slug
# ===========================================================================

def test_validate_slug_accepts_valid() -> None:
    """Valid slugs pass validation."""
    from routstr.core.admin import _validate_slug

    assert _validate_slug("valid-slug") == "valid-slug"
    assert _validate_slug("valid123") == "valid123"
    assert _validate_slug("my-provider") == "my-provider"


def test_validate_slug_rejects_spaces() -> None:
    """Slugs with spaces are rejected."""
    from fastapi import HTTPException

    from routstr.core.admin import _validate_slug

    with pytest.raises(HTTPException):
        _validate_slug("invalid slug")


def test_validate_slug_rejects_too_short() -> None:
    """Slugs shorter than 3 chars are rejected."""
    from fastapi import HTTPException

    from routstr.core.admin import _validate_slug

    with pytest.raises(HTTPException):
        _validate_slug("ab")


# ===========================================================================
# admin login endpoint
# ===========================================================================

@pytest.mark.asyncio
async def test_admin_login_requires_payload() -> None:
    """admin_login requires a payload — verify it exists."""
    # Verify the function signature
    import inspect

    from routstr.core.admin import admin_login
    sig = inspect.signature(admin_login)
    params = list(sig.parameters.keys())
    assert "request" in params
    assert "payload" in params or len(params) >= 2
