"""Reveal the node's Nostr identity (``nsec``) from the encrypted secret store.

The admin API intentionally redacts the nsec, and the node no longer prints it at
startup — stdout is captured by ``docker compose logs``. This is the operator
escape hatch for recovering the identity: it decrypts the ``encrypted_nsec``
column using the node's master key.

Requires the same ``ROUTSTR_SECRET_KEY`` (or persisted key file) that encrypted
the value. A missing or changed key fails with a clear error; there is no
recovery without the original key.

    python scripts/reveal_nsec.py
        Print the stored npub and nsec to stdout.

Treat the output as highly sensitive: the nsec is full control of the node's
Nostr identity.
"""

import argparse
import asyncio
import sys

from cryptography.fernet import InvalidToken
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import create_session, get_secret
from routstr.core.settings import derive_npub_from_nsec
from routstr.core.vault import decrypt


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="reveal_nsec",
        description="Decrypt and print the node's stored Nostr nsec.",
    )


async def reveal_nsec(session: AsyncSession) -> str:
    """Return the decrypted nsec stored in ``session``'s database.

    Raises ``ValueError`` when no identity is stored (never configured, or
    intentionally cleared), and ``InvalidToken`` when the configured
    ``ROUTSTR_SECRET_KEY`` is not the one the value was encrypted under.
    """
    secret = await get_secret(session)
    if not secret.encrypted_nsec:
        raise ValueError(
            "No nsec is stored (never configured, or intentionally cleared)."
        )
    return decrypt(secret.encrypted_nsec)


async def _run() -> str:
    async with create_session() as session:
        return await reveal_nsec(session)


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)

    try:
        nsec = asyncio.run(_run())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except InvalidToken:
        print(
            "error: stored nsec cannot be decrypted with the current "
            "ROUTSTR_SECRET_KEY. Restore the original key.",
            file=sys.stderr,
        )
        return 2
    except RuntimeError as exc:
        # Raised by the vault when no key is configured or the key is malformed.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    npub = derive_npub_from_nsec(nsec)
    print(f"npub: {npub or '(unable to derive npub)'}")
    print(f"nsec: {nsec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
