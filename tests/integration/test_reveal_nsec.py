"""Tests for the ``reveal_nsec`` recovery script.

The node never prints the nsec at startup (stdout is captured by
``docker compose logs``), so this script is how an operator recovers the
identity: it decrypts the ``encrypted_nsec`` column with ``ROUTSTR_SECRET_KEY``.
It must fail clearly when no identity exists or the key is wrong, and print both
npub and nsec when it succeeds.
"""

import pytest
from cryptography.fernet import InvalidToken
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import set_nsec
from routstr.core.settings import derive_npub_from_nsec
from scripts.reveal_nsec import main, reveal_nsec

# Must match the alternate key in tests/conftest.py.
TEST_SECRET_KEY_ALT = "_Teyrky_iToeDK51Tj1FsI9MJ340_cqKGmeher-a7MQ="

NSEC_HEX = "1" * 64


@pytest.mark.asyncio
async def test_reveal_returns_the_stored_nsec(
    integration_session: AsyncSession,
) -> None:
    await set_nsec(integration_session, NSEC_HEX)

    assert await reveal_nsec(integration_session) == NSEC_HEX


@pytest.mark.asyncio
async def test_reveal_errors_when_no_nsec_is_stored(
    integration_session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="No nsec is stored"):
        await reveal_nsec(integration_session)


@pytest.mark.asyncio
async def test_reveal_fails_with_the_wrong_key(
    integration_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await set_nsec(integration_session, NSEC_HEX)
    monkeypatch.setenv("ROUTSTR_SECRET_KEY", TEST_SECRET_KEY_ALT)

    with pytest.raises(InvalidToken):
        await reveal_nsec(integration_session)


def test_main_prints_npub_and_nsec(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run() -> str:
        return NSEC_HEX

    monkeypatch.setattr("scripts.reveal_nsec._run", _fake_run)

    assert main([]) == 0
    out = capsys.readouterr().out
    assert f"npub: {derive_npub_from_nsec(NSEC_HEX)}" in out
    assert f"nsec: {NSEC_HEX}" in out


def test_main_reports_no_nsec_without_traceback(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run() -> str:
        raise ValueError("No nsec is stored")

    monkeypatch.setattr("scripts.reveal_nsec._run", _fake_run)

    assert main([]) == 2
    assert "No nsec is stored" in capsys.readouterr().err
