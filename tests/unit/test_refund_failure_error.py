"""The refund endpoint's refusal reasons must stay machine-readable.

Clients act on these codes. ``no_balance_to_refund`` proves the key holds
nothing and its stored copy can be dropped; ``refund_ongoing_requests`` is a
transient race whose balance is still on the key and must be kept;
``balance_too_small_to_refund`` is dust that no retry can fix. They used to be
bare ``detail`` strings, which are indistinguishable to a client, so the SDK
kept every dead key and re-swept it forever.
"""

import pytest
from fastapi import HTTPException

from routstr import refund


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (refund.REFUND_NO_BALANCE, "No balance to refund"),
        (refund.REFUND_BALANCE_TOO_SMALL, "Balance too small to refund"),
        (
            refund.REFUND_ONGOING_REQUESTS,
            "Cannot refund key. There are ongoing requests for this api key.",
        ),
    ],
)
def test_refund_failure_error_carries_its_code(code: str, message: str) -> None:
    error = refund.refund_failure_error(message, code)

    assert isinstance(error, HTTPException)
    assert error.status_code == 400
    assert error.detail == {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "code": code,
        }
    }


def test_refund_failure_codes_are_distinct_lowercase_slugs() -> None:
    codes = {
        refund.REFUND_NO_BALANCE,
        refund.REFUND_BALANCE_TOO_SMALL,
        refund.REFUND_ONGOING_REQUESTS,
    }

    assert len(codes) == 3
    assert all(code == code.lower() for code in codes)
    assert all(code.strip() == code for code in codes)
