"""Recover admin access by resetting the stored admin password (issue #553).

The lockout escape hatch for an operator who has lost the admin password. It
talks to the ``secrets`` table directly and deliberately does *not* require
``ROUTSTR_SECRET_KEY``: the admin password is scrypt-hashed (key-independent),
so recovery works even when the encryption key is missing or has changed.

Two explicit, mutually exclusive actions — running with no arguments only prints
help, so the password can't be reset by accident:

    python scripts/reset_admin_password.py --password <new-password>
        Hash and store <new-password> now.

    python scripts/reset_admin_password.py --regenerate
        Clear the stored hash; the next node startup generates a fresh random
        password and logs it once (with the /admin URL).

Add ``--revoke-cli-tokens`` (alone or with either action) to also revoke every
CLI token, e.g. when the password was reset because the node may be
compromised. Dashboard sessions live in the running node's memory, so they end
when the node restarts (or after at most an hour).
"""

import argparse
import asyncio
import sys
import time

from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import (
    create_session,
    get_secret,
    revoke_all_cli_tokens,
    set_admin_password,
)
from routstr.core.vault import MIN_PASSWORD_LENGTH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reset_admin_password",
        description="Reset the node's admin password (recovery from lockout).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--password",
        metavar="NEW_PASSWORD",
        help=f"set this as the new admin password (min {MIN_PASSWORD_LENGTH} chars)",
    )
    action.add_argument(
        "--regenerate",
        action="store_true",
        help="clear the password so the next startup generates and logs a new one",
    )
    parser.add_argument(
        "--revoke-cli-tokens",
        action="store_true",
        help="also revoke every CLI token",
    )
    return parser


async def apply_reset(
    session: AsyncSession,
    *,
    password: str | None = None,
    regenerate: bool = False,
    revoke_cli_tokens: bool = False,
) -> str:
    """Perform the requested reset against ``session``; return a status message."""
    messages: list[str] = []
    if password is not None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"New password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        await set_admin_password(session, password)
        messages.append(
            "Admin password updated. Restart the node to end existing dashboard "
            "sessions."
        )

    if regenerate:
        secret = await get_secret(session)
        secret.admin_password_hash = None
        secret.updated_at = int(time.time())
        session.add(secret)
        await session.commit()
        messages.append(
            "Admin password cleared. The next node startup will generate a new "
            "one and log it once with the /admin URL."
        )

    if revoke_cli_tokens:
        revoked = await revoke_all_cli_tokens(session)
        messages.append(f"Revoked {revoked} CLI token(s).")

    return "\n".join(messages)


async def _run(password: str | None, regenerate: bool, revoke_cli_tokens: bool) -> str:
    async with create_session() as session:
        return await apply_reset(
            session,
            password=password,
            regenerate=regenerate,
            revoke_cli_tokens=revoke_cli_tokens,
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.password is None and not args.regenerate and not args.revoke_cli_tokens:
        parser.print_help()
        return 0

    try:
        message = asyncio.run(
            _run(args.password, args.regenerate, args.revoke_cli_tokens)
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
