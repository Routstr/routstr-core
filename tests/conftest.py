"""Shared pytest configuration for the whole suite.

A fixed, valid ``ROUTSTR_SECRET_KEY`` is set before any app import so that
secret encryption is deterministic across the suite and the mandatory-key
fail-fast does not break app-boot tests. Tests that need a different key (or an
absent one) override this per-test via ``monkeypatch``.
"""

import os

# Valid Fernet keys; KEY_A is the suite default, KEY_B is for wrong-key tests.
TEST_SECRET_KEY = "l_Tkp-7xmjcQ-IFhr6qhILrU8HPRbEmYMrfSbo_5srU="
TEST_SECRET_KEY_ALT = "_Teyrky_iToeDK51Tj1FsI9MJ340_cqKGmeher-a7MQ="

os.environ.setdefault("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY)
