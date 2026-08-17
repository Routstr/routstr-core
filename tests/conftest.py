"""Shared pytest configuration for the whole suite.

A fixed, valid ``ROUTSTR_SECRET_KEY`` is set before any app import so that
secret encryption is deterministic across the suite and the mandatory-key
fail-fast does not break app-boot tests. Tests that need a different key (or an
absent one) override this per-test via ``monkeypatch``.
"""

import os
from typing import Iterator

import pytest

# Valid Fernet keys; KEY_A is the suite default, KEY_B is for wrong-key tests.
TEST_SECRET_KEY = "l_Tkp-7xmjcQ-IFhr6qhILrU8HPRbEmYMrfSbo_5srU="
TEST_SECRET_KEY_ALT = "_Teyrky_iToeDK51Tj1FsI9MJ340_cqKGmeher-a7MQ="

os.environ.setdefault("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)


@pytest.fixture(autouse=True)
def _isolate_redemption_negative_cache() -> Iterator[None]:
    """Clear the process-wide negative cache between tests.

    The cache deliberately persists terminal redemption failures across
    requests; without this fixture a test that burns a token would poison
    every later test reusing the same token string.
    """
    from routstr.redemption_cache import redemption_negative_cache

    redemption_negative_cache.clear()
    yield
    redemption_negative_cache.clear()
