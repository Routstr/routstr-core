"""Tests for the ``reset_admin_password`` recovery script (issue #553).

The script is the lockout escape hatch: it works without ``ROUTSTR_SECRET_KEY``
(scrypt hashing is key-independent). Two explicit, mutually exclusive actions —
``--password`` sets a new hash now, ``--regenerate`` clears the hash so the next
boot generates and logs a fresh one. A bare invocation is informational only and
must never touch the database (so nobody resets their password by accident).
"""

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core import vault
from routstr.core.db import get_secret, set_admin_password
from scripts.reset_admin_password import apply_reset, build_parser, main


@pytest.mark.asyncio
async def test_password_sets_a_verifiable_hash(
    integration_session: AsyncSession,
) -> None:
    await apply_reset(integration_session, password="recover-me-123")

    secret = await get_secret(integration_session)
    assert secret.admin_password_hash is not None
    assert vault.verify_password("recover-me-123", secret.admin_password_hash) is True
    assert secret.updated_at is not None


@pytest.mark.asyncio
async def test_regenerate_clears_the_hash(
    integration_session: AsyncSession,
) -> None:
    # Start from a node that already has an admin password set.
    await set_admin_password(integration_session, "old-password-9")
    assert (await get_secret(integration_session)).admin_password_hash is not None

    await apply_reset(integration_session, regenerate=True)

    secret = await get_secret(integration_session)
    # Cleared -> the next boot's bootstrap_secrets generates and logs a new one.
    assert secret.admin_password_hash is None
    assert secret.updated_at is not None


@pytest.mark.asyncio
async def test_password_below_min_length_is_rejected(
    integration_session: AsyncSession,
) -> None:
    await set_admin_password(integration_session, "old-password-9")

    with pytest.raises(ValueError, match="8 characters"):
        await apply_reset(integration_session, password="short")

    # The existing password is untouched by the rejected reset.
    secret = await get_secret(integration_session)
    assert vault.verify_password("old-password-9", secret.admin_password_hash or "")


def test_password_and_regenerate_are_mutually_exclusive() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--password", "abcd1234", "--regenerate"])


def test_no_args_prints_help_and_never_opens_a_session(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail() -> None:
        raise AssertionError("a bare invocation must not touch the database")

    monkeypatch.setattr("scripts.reset_admin_password.create_session", _fail)

    assert main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()
