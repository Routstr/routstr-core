"""Tests for the ``create_cli_token`` script.

Like ``reset_admin_password.py`` it writes the node's own ``cli_tokens`` table
directly, so it can bootstrap the CLI without the admin password. It must create
a usable long-lived token, honour ``--replace`` and ``--expires-in-days``, and
merge ``{node_url, token}`` into the CLI config with owner-only permissions.
"""

import json
import time
from pathlib import Path

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import CliToken
from scripts.create_cli_token import create_token, main, write_cli_config


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_token_persists_and_never_expires(
    integration_session: AsyncSession,
) -> None:
    token = await create_token(integration_session, name="agent")

    rows = (
        await integration_session.exec(select(CliToken).where(CliToken.token == token))
    ).all()
    assert len(rows) == 1
    assert rows[0].name == "agent"
    assert rows[0].expires_at is None
    assert len(token) >= 32


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_token_with_expiry(integration_session: AsyncSession) -> None:
    before = int(time.time())
    await create_token(integration_session, name="ci", expires_in_days=7)

    row = (
        await integration_session.exec(select(CliToken).where(CliToken.name == "ci"))
    ).all()[0]
    assert row.expires_at is not None
    assert before + 7 * 86400 <= row.expires_at <= int(time.time()) + 7 * 86400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replace_revokes_prior_tokens_with_same_name(
    integration_session: AsyncSession,
) -> None:
    first = await create_token(integration_session, name="agent")
    second = await create_token(integration_session, name="agent", replace=True)

    assert first != second
    rows = (
        await integration_session.exec(select(CliToken).where(CliToken.name == "agent"))
    ).all()
    assert len(rows) == 1
    assert rows[0].token == second


def test_write_cli_config_merges_and_sets_owner_only_mode(tmp_path: Path) -> None:
    config = tmp_path / ".routstr" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"keep": "me", "node_url": "http://old"}))

    write_cli_config(config, "tok-123", "https://node.example")

    assert json.loads(config.read_text()) == {
        "keep": "me",
        "node_url": "https://node.example",
        "token": "tok-123",
    }
    assert config.stat().st_mode & 0o777 == 0o600


def test_main_print_token_writes_only_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _fake_run(name: str, expires_in_days: int | None, replace: bool) -> str:
        assert name == "agent"
        assert expires_in_days is None
        assert replace is True
        return "tok-abc"

    monkeypatch.setattr("scripts.create_cli_token._run", _fake_run)

    assert main(["--name", "agent", "--replace", "--print-token"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "tok-abc"


def test_main_writes_cli_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_run(name: str, expires_in_days: int | None, replace: bool) -> str:
        return "tok-abc"

    monkeypatch.setattr("scripts.create_cli_token._run", _fake_run)
    config = tmp_path / "config.json"

    code = main(
        ["--name", "agent", "--config", str(config), "--node-url", "https://node.example"]
    )

    assert code == 0
    assert json.loads(config.read_text()) == {
        "node_url": "https://node.example",
        "token": "tok-abc",
    }


def test_parser_rejects_empty_name_and_bad_expiry() -> None:
    with pytest.raises(SystemExit):
        main(["--name", "   "])
    with pytest.raises(SystemExit):
        main(["--expires-in-days", "0"])
