"""Unit tests for the logging SecurityFilter.

This module tests that the SecurityFilter correctly identifies and redacts
sensitive information from log messages without causing false positives.

"""

import logging
from collections.abc import Callable

import pytest

from routstr.core.logging import SecurityFilter


@pytest.fixture
def security_filter() -> SecurityFilter:
    """Provide an instance of the SecurityFilter for testing."""
    return SecurityFilter()


@pytest.fixture
def filter_message(security_filter: SecurityFilter) -> Callable[[str], str]:
    """A helper fixture to apply the filter to a message string."""

    def _filter(msg: str) -> str:
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )
        security_filter.filter(record)
        return record.getMessage()

    return _filter


def test_redacts_unquoted_key_value_pairs(filter_message: Callable[[str], str]) -> None:
    """Test that an unquoted key-value pair is correctly redacted."""
    original = "Processing request with api_key=sk-12345abcdef"
    expected = "Processing request with api_key: [REDACTED]"
    assert filter_message(original) == expected


def test_redacts_quoted_key_value_pairs(filter_message: Callable[[str], str]) -> None:
    """Test that a quoted token is correctly redacted."""
    original = 'User authenticated with token="cashuA123abc"'
    expected = "User authenticated with token: [REDACTED]"
    assert filter_message(original) == expected


def test_redacts_bearer_token(filter_message: Callable[[str], str]) -> None:
    """Test that a Bearer token of sufficient length is redacted."""
    original = "Authorization: Bearer abc1234567890xyzabcdefg"
    expected = "Authorization: [REDACTED]"
    assert filter_message(original) == expected


def test_redacts_cashu_token(filter_message: Callable[[str], str]) -> None:
    """Test that a Cashu token is redacted."""
    original = "Received cashuTOKENeyJ0b2tlbiI6W3siaWQiOiI"
    expected = "Received [REDACTED]"
    assert filter_message(original) == expected


def test_redacts_nsec_key(filter_message: Callable[[str], str]) -> None:
    """Test that a full-length Nostr private key is redacted."""
    original = "Private key is nsec1a8d9f8s7d9f8a7s6d5f4a3s2d1f9a8s7d6f5a4s3d2f1a9s8d7f6a5s4d3f"
    expected = "Private key is [REDACTED]"
    assert filter_message(original) == expected


def test_ignores_non_sensitive_message(filter_message: Callable[[str], str]) -> None:
    """Test that a message with no sensitive data is left untouched."""
    original = "No token pricing configured, using base cost"
    expected = "No token pricing configured, using base cost"
    assert filter_message(original) == expected


def test_multiple_secrets_in_one_message(filter_message: Callable[[str], str]) -> None:
    """Test that multiple different secrets in one message are all redacted."""
    original = 'Auth with Bearer abcdefghijklmnopqrstuvwxyz and api_key="sk-12345"'
    expected = "Auth with [REDACTED] and api_key: [REDACTED]"
    assert filter_message(original) == expected


def test_redacts_key_with_no_value(filter_message: Callable[[str], str]) -> None:
    """Test that a key with no value is not redacted."""
    original = "Request contains api_key and secret."
    expected = "Request contains api_key and secret."
    assert filter_message(original) == expected


def test_redacts_key_value_with_spaces(filter_message: Callable[[str], str]) -> None:
    """Test that key-value pairs with extra spaces are correctly redacted."""
    original = "Auth info:   api_key   =   'sk-12345'"
    expected = "Auth info:   api_key: [REDACTED]"
    assert filter_message(original) == expected


def test_is_case_insensitive_for_keys(filter_message: Callable[[str], str]) -> None:
    """Test that key matching is case-insensitive."""
    original = "TOKEN=sk-abcdef12345"
    expected = "token: [REDACTED]"
    assert filter_message(original) == expected


def test_is_case_insensitive_for_standalone(
    filter_message: Callable[[str], str],
) -> None:
    """Test that standalone matching is case-insensitive."""
    original = "Using NSEC1a8d9f8s7d9f8a7s6d5f4a3s2d1f9a8s7d6f5a4s3d2f1a9s8d7f6a5s4d3f and CaShuA123abc"
    expected = "Using [REDACTED] and [REDACTED]"
    assert filter_message(original) == expected
