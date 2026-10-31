#!/usr/bin/env python3
"""Copy a Routstr SQLite application database into an empty PostgreSQL database.

Usage:

    uv run python scripts/migrate_sqlite_to_postgres.py \\
        --source keys.db \\
        --target postgresql+asyncpg://user:pass@host:5432/routstr [--dry-run]

The copy itself lives in ``routstr.core.sqlite_import``, which the node also runs
at startup when ``SQLITE_IMPORT_PATH`` is set; its docstring lists every step.
``--dry-run`` performs every step and rolls the copy back at the end; the empty
schema stays, which a later real run accepts as empty.

The node must be pointed at the new database with ``DATABASE_URL`` and must keep
the same ``ROUTSTR_SECRET_KEY`` (or key file); encrypted secrets are copied
verbatim and are unreadable without it. The Cashu wallet stores under
``.wallet/`` stay SQLite and are not touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("UPSTREAM_BASE_URL", "http://migration.invalid")

from routstr.core.sqlite_import import (  # noqa: E402
    MigrationError,
    _coerce,
    copy_sqlite_to_postgres,
)

__all__ = ["MigrationError", "_coerce", "main"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", required=True, help="path to the SQLite keys.db")
    parser.add_argument(
        "--target", required=True, help="postgresql+asyncpg://user:pass@host/db"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="copy and verify, then roll the copy back (the empty schema stays)",
    )
    parser.add_argument(
        "--allow-drop",
        action="append",
        metavar="TABLE[.COLUMN]",
        help="explicitly discard a source table or column with data the target "
        "schema does not have",
    )
    parser.add_argument(
        "--report", type=Path, help="also write the JSON report to this file"
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(
            copy_sqlite_to_postgres(
                Path(args.source),
                args.target,
                dry_run=args.dry_run,
                allow_drop=args.allow_drop or [],
            )
        )
    except MigrationError as exc:
        print(f"migration aborted: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, indent=2, default=str)
    if args.report:
        args.report.write_text(rendered + "\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
