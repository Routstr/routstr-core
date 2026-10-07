"""Create a long-lived CLI/agent bearer token for this node.

Bootstraps the Routstr CLI without needing the admin password: like
``reset_admin_password.py`` it writes the node's own ``cli_tokens`` table
directly. The token is accepted by admin endpoints as
``Authorization: Bearer <token>`` (see ``require_admin_api``), exactly like a
short-lived admin session — but it is persisted, optional-expiry and revocable.

    python scripts/create_cli_token.py --name agent
        Create a token and print it once.

    python scripts/create_cli_token.py --name agent --replace \
        --node-url https://node.example --config ~/.routstr/config.json
        Also merge it into the CLI config (mode 0600).

    python scripts/create_cli_token.py --name agent --print-token
        Print only the token on stdout (for scripts); notices go to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import CliToken, create_session

DEFAULT_CONFIG = Path.home() / ".routstr" / "config.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create_cli_token",
        description="Create a long-lived CLI/agent token for this node.",
    )
    parser.add_argument(
        "--name", default="cli", help="label for the token (default: cli)"
    )
    parser.add_argument(
        "--expires-in-days",
        type=int,
        default=None,
        metavar="N",
        help="expire the token after N days (default: never expires)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="revoke existing tokens with the same --name first",
    )
    parser.add_argument(
        "--node-url", help="node URL to store in the CLI config"
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="merge {node_url, token} into this CLI config file (mode 0600)",
    )
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="print only the token on stdout (for scripts)",
    )
    return parser


async def create_token(
    session: AsyncSession,
    *,
    name: str,
    expires_in_days: int | None = None,
    replace: bool = False,
) -> str:
    """Create and persist a CLI token; return the raw token value."""
    if replace:
        existing = await session.exec(select(CliToken).where(CliToken.name == name))
        for row in existing.all():
            await session.delete(row)
    raw_token = secrets.token_urlsafe(32)
    expires_at: int | None = None
    if expires_in_days is not None and expires_in_days > 0:
        expires_at = int(time.time()) + expires_in_days * 86400
    session.add(CliToken(token=raw_token, name=name, expires_at=expires_at))
    await session.commit()
    return raw_token


def write_cli_config(path: Path, token: str, node_url: str | None = None) -> None:
    """Merge ``{node_url, token}`` into the CLI config at ``path`` (mode 0600)."""
    data: dict[str, object] = {}
    try:
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    data["token"] = token
    if node_url:
        data["node_url"] = node_url
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    os.chmod(path, 0o600)


async def _run(name: str, expires_in_days: int | None, replace: bool) -> str:
    async with create_session() as session:
        return await create_token(
            session, name=name, expires_in_days=expires_in_days, replace=replace
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    name = args.name.strip()
    if not name:
        parser.error("--name must not be empty")
    if args.expires_in_days is not None and args.expires_in_days <= 0:
        parser.error("--expires-in-days must be positive")

    try:
        token = asyncio.run(_run(name, args.expires_in_days, args.replace))
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    notices: list[str] = []
    if args.config is not None:
        write_cli_config(args.config, token, args.node_url)
        notices.append(f"Saved CLI config: {args.config} (0600)")

    if args.print_token:
        for notice in notices:
            print(notice, file=sys.stderr)
        print(token)
    else:
        print(f"Created CLI token '{name}'.")
        print(f"  token: {token}")
        for notice in notices:
            print(f"  {notice}")
        if args.config is None:
            print("  save it with: routstr init --node-url <url> --token <token>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
