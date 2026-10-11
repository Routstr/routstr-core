#!/usr/bin/env python3
"""Copy a Routstr SQLite application database into an empty PostgreSQL database.

Usage:

    uv run python scripts/migrate_sqlite_to_postgres.py \\
        --source keys.db \\
        --target postgresql+asyncpg://user:pass@host:5432/routstr [--dry-run]

What it does, in order:

1. Takes a point-in-time snapshot of the source with SQLite's online backup API,
   so a running node cannot change rows mid-copy, and runs ``integrity_check``.
2. Refuses a target that already holds application rows, then builds the target
   schema with the repo's Alembic chain (``upgrade head``) plus the runtime
   ``settings`` table.
3. Pre-flights every table: unknown non-empty tables, source columns the target
   does not have but which hold data, target NOT NULL columns the source cannot
   fill, orphaned foreign keys, NUL bytes in text and lossy type coercions all
   abort the run instead of being dropped silently.
4. Copies all rows in foreign-key order inside one PostgreSQL transaction,
   advances every serial sequence past the copied ids, then reads every row back
   and compares a per-table fingerprint and money totals with the snapshot. Any
   mismatch rolls the whole transaction back.

``--dry-run`` performs every step and rolls the copy back at the end; the empty
schema built in step 2 stays, which a later real run accepts as empty.

The node must be pointed at the new database with ``DATABASE_URL`` and must keep
the same ``ROUTSTR_SECRET_KEY`` (or key file); encrypted secrets are copied
verbatim and are unreadable without it. The Cashu wallet stores under
``.wallet/`` stay SQLite and are not touched.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_DDL = (
    "CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT "
    "NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
)
SQLITE_INTERNAL_TABLES = {"sqlite_sequence", "sqlite_stat1", "alembic_version"}
# Rows the Alembic chain itself inserts; a target holding only these, untouched,
# still counts as empty.
PRISTINE_SEED_ROWS = {
    "routstr_fees": (
        "accumulated_msats = 0 AND total_paid_msats = 0 "
        "AND payout_in_progress_msats = 0"
    ),
}
BATCH_SIZE = 1000
# Totals that must survive the copy exactly; a fingerprint mismatch already
# fails the run, these make the report legible for an operator.
MONEY_TOTALS = {
    "api_keys": ["balance", "reserved_balance", "total_spent", "total_requests"],
    "cashu_transactions": ["amount"],
    "refunds": ["amount_msats"],
    "reservation_releases": ["reserved_msats"],
    "routstr_fees": ["accumulated_msats", "total_paid_msats"],
    "lightning_invoices": ["amount_sats"],
    "cashu_swaps": ["source_amount", "destination_amount"],
}


class MigrationError(RuntimeError):
    pass


@dataclass
class TablePlan:
    name: str
    columns: list[str]
    dropped_empty_columns: list[str] = field(default_factory=list)
    source_rows: int = 0


def _snapshot(source: Path, workdir: Path) -> Path:
    if not source.exists():
        raise MigrationError(f"source database not found: {source}")
    snapshot = workdir / "source-snapshot.sqlite3"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(snapshot)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    conn = sqlite3.connect(snapshot)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise MigrationError(f"source integrity_check failed: {result}")
    return snapshot


def _sqlite_tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    return {
        name: [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')]
        for name in names
    }


def _source_revision(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _upgrade_target_schema(target_url: str) -> None:
    """Build the schema with the same Alembic chain the node runs at startup."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", target_url)
    command.upgrade(cfg, "head")


def _is_type(column_type: Any, *kinds: type) -> bool:
    return isinstance(column_type, kinds)


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _coerce(value: Any, column: sa.Column[Any], where: str) -> Any:
    """Convert a raw SQLite value to what the PostgreSQL column stores.

    Raises instead of guessing whenever the conversion could lose information.
    """
    if value is None:
        return None
    ctype = column.type
    if _is_type(ctype, sa.Boolean):
        if value in (0, 1, True, False):
            return bool(value)
        if isinstance(value, str) and value.lower() in {"0", "1", "true", "false"}:
            return value.lower() in {"1", "true"}
        raise MigrationError(f"{where}: {value!r} is not a boolean")
    if _is_type(ctype, sa.Integer):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        raise MigrationError(f"{where}: {value!r} is not an integer")
    if _is_type(ctype, sa.Float, sa.Numeric):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            raise MigrationError(f"{where}: {value!r} is not a number") from None
    if _is_type(ctype, sa.DateTime):
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError:
                raise MigrationError(f"{where}: {value!r} is not a datetime") from None
        timezone_aware = bool(getattr(ctype, "timezone", False))
        if timezone_aware:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return _to_naive_utc(parsed)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            raise MigrationError(f"{where}: non-UTF-8 bytes in a text column") from None
    if not isinstance(value, str):
        value = str(value)
    if "\x00" in value:
        raise MigrationError(f"{where}: NUL byte, which PostgreSQL text rejects")
    return value


def _normalize(value: Any) -> Any:
    """Backend-neutral form of a stored value, for the fingerprint."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime):
        return _to_naive_utc(value).isoformat()
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return value
    return str(value)


def _fingerprint(rows: list[tuple[Any, ...]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(json.dumps([_normalize(v) for v in row]) for row in rows):
        digest.update(row.encode())
        digest.update(b"\n")
    return digest.hexdigest()


async def _target_has_rows(conn: AsyncConnection, tables: list[str]) -> list[str]:
    existing = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
    occupied = []
    for name in tables:
        if name not in existing:
            continue
        query = f'SELECT count(*) FROM "{name}"'
        if name in PRISTINE_SEED_ROWS:
            query += f" WHERE NOT ({PRISTINE_SEED_ROWS[name]})"
        count = await conn.scalar(sa.text(query))
        if count:
            occupied.append(f"{name} ({count} rows)")
    return occupied


def _plan(
    src: sqlite3.Connection,
    source_tables: dict[str, list[str]],
    target: sa.MetaData,
    ordered: list[str],
    allow_drop: set[str],
) -> list[TablePlan]:
    problems: list[str] = []
    plans: list[TablePlan] = []

    for name, _cols in source_tables.items():
        if name in SQLITE_INTERNAL_TABLES or name in target.tables:
            continue
        count = src.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        if count and name not in allow_drop:
            problems.append(
                f"source table {name} has {count} rows and no target table "
                f"(pass --allow-drop {name} to discard it)"
            )

    for name in ordered:
        table = target.tables[name]
        if name not in source_tables:
            continue
        source_cols = source_tables[name]
        target_cols = [c.name for c in table.columns]
        plan = TablePlan(
            name=name, columns=[c for c in source_cols if c in target_cols]
        )
        plan.source_rows = src.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]

        for extra in (c for c in source_cols if c not in target_cols):
            non_null = src.execute(
                f'SELECT count(*) FROM "{name}" WHERE "{extra}" IS NOT NULL'
            ).fetchone()[0]
            if non_null and f"{name}.{extra}" not in allow_drop:
                problems.append(
                    f"{name}.{extra} holds data in {non_null} rows but the target "
                    f"schema has no such column (pass --allow-drop {name}.{extra})"
                )
            plan.dropped_empty_columns.append(f"{extra} ({non_null} non-null)")

        for column in table.columns:
            if column.name in plan.columns:
                continue
            fillable = (
                column.nullable
                or column.server_default is not None
                or column.autoincrement is True
            )
            if not fillable and plan.source_rows:
                problems.append(
                    f"{name}.{column.name} is NOT NULL in the target with no "
                    "default and the source has no such column"
                )
        plans.append(plan)

    for name in ordered:
        table = target.tables[name]
        if name not in source_tables:
            continue
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent not in source_tables:
                continue
            child_col, parent_col = fk.parent.name, fk.column.name
            if child_col not in source_tables[name]:
                continue
            orphans = src.execute(
                f'SELECT count(*) FROM "{name}" c WHERE c."{child_col}" IS NOT NULL '
                f'AND NOT EXISTS (SELECT 1 FROM "{parent}" p '
                f'WHERE p."{parent_col}" = c."{child_col}")'
            ).fetchone()[0]
            if orphans:
                problems.append(
                    f"{name}.{child_col} has {orphans} rows pointing at missing "
                    f"{parent}.{parent_col}; PostgreSQL enforces this foreign key"
                )

    if problems:
        raise MigrationError("pre-flight failed:\n  - " + "\n  - ".join(problems))
    return plans


async def _copy_table(
    conn: AsyncConnection,
    src: sqlite3.Connection,
    table: sa.Table,
    plan: TablePlan,
) -> list[tuple[Any, ...]]:
    """Insert every source row; return the coerced rows for verification."""
    quoted = ", ".join(f'"{c}"' for c in plan.columns)
    cursor = src.execute(f'SELECT {quoted} FROM "{plan.name}"')
    coerced_rows: list[tuple[Any, ...]] = []
    while True:
        batch = cursor.fetchmany(BATCH_SIZE)
        if not batch:
            break
        payload = []
        for raw in batch:
            row = {
                col: _coerce(value, table.columns[col], f"{plan.name}.{col}")
                for col, value in zip(plan.columns, raw)
            }
            payload.append(row)
            coerced_rows.append(tuple(row[c] for c in plan.columns))
        await conn.execute(table.insert(), payload)
    return coerced_rows


async def _reset_sequences(conn: AsyncConnection, target: sa.MetaData) -> list[str]:
    advanced = []
    for table in target.sorted_tables:
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or not _is_type(pk[0].type, sa.Integer):
            continue
        sequence = await conn.scalar(
            sa.text("SELECT pg_get_serial_sequence(:t, :c)"),
            {"t": f'"{table.name}"', "c": pk[0].name},
        )
        if not sequence:
            continue
        max_id = await conn.scalar(
            sa.text(f'SELECT max("{pk[0].name}") FROM "{table.name}"')
        )
        if max_id is None:
            await conn.execute(sa.text("SELECT setval(:s, 1, false)"), {"s": sequence})
        else:
            await conn.execute(
                sa.text("SELECT setval(:s, :v, true)"), {"s": sequence, "v": max_id}
            )
        advanced.append(f"{sequence} -> {max_id}")
    return advanced


async def _verify(
    conn: AsyncConnection,
    src: sqlite3.Connection,
    plans: list[TablePlan],
    expected: dict[str, list[tuple[Any, ...]]],
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    mismatches = []
    for plan in plans:
        quoted = ", ".join(f'"{c}"' for c in plan.columns)
        result = await conn.execute(sa.text(f'SELECT {quoted} FROM "{plan.name}"'))
        target_rows = [tuple(r) for r in result.all()]
        source_fp = _fingerprint(expected[plan.name])
        target_fp = _fingerprint(target_rows)
        entry: dict[str, Any] = {
            "rows": {"source": plan.source_rows, "target": len(target_rows)},
            "fingerprint_match": source_fp == target_fp,
        }
        if plan.dropped_empty_columns:
            entry["dropped_columns"] = plan.dropped_empty_columns
        totals = {}
        for col in MONEY_TOTALS.get(plan.name, []):
            if col not in plan.columns:
                continue
            source_total = src.execute(
                f'SELECT coalesce(sum("{col}"), 0) FROM "{plan.name}"'
            ).fetchone()[0]
            target_total = await conn.scalar(
                sa.text(f'SELECT coalesce(sum("{col}"), 0) FROM "{plan.name}"')
            )
            totals[col] = {"source": int(source_total), "target": int(target_total)}
            if int(source_total) != int(target_total):
                mismatches.append(f"{plan.name}.{col} total")
        if totals:
            entry["totals"] = totals
        if plan.source_rows != len(target_rows):
            mismatches.append(f"{plan.name} row count")
        if source_fp != target_fp:
            mismatches.append(f"{plan.name} fingerprint")
        report[plan.name] = entry
    if mismatches:
        raise MigrationError(
            "verification failed, transaction rolled back: " + ", ".join(mismatches)
        )
    return report


async def migrate(args: argparse.Namespace) -> dict[str, Any]:
    target_url = make_url(args.target)
    if target_url.get_backend_name() != "postgresql":
        raise MigrationError("--target must be a postgresql+asyncpg:// URL")
    if target_url.drivername != "postgresql+asyncpg":
        target_url = target_url.set(drivername="postgresql+asyncpg")
    target_dsn = target_url.render_as_string(hide_password=False)
    allow_drop = set(args.allow_drop or [])

    with tempfile.TemporaryDirectory(prefix="routstr-pg-migrate-") as tmp:
        snapshot = _snapshot(Path(args.source).expanduser().resolve(), Path(tmp))
        src = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            source_tables = _sqlite_tables(src)
            source_revision = _source_revision(src)

            # The app's metadata is the authority on which tables exist; importing
            # it needs the target URL in place before routstr.core.db loads.
            os.environ["DATABASE_URL"] = target_dsn
            from sqlmodel import SQLModel

            import routstr.core.db  # noqa: F401  (registers every table)

            app_tables = [t.name for t in SQLModel.metadata.sorted_tables]
            engine = create_async_engine(target_dsn)
            try:
                async with engine.connect() as conn:
                    occupied = await _target_has_rows(conn, app_tables + ["settings"])
                if occupied:
                    raise MigrationError(
                        "target is not empty, refusing to merge into it: "
                        + ", ".join(occupied)
                    )

                await asyncio.to_thread(_upgrade_target_schema, target_dsn)
                async with engine.begin() as conn:
                    await conn.execute(sa.text(SETTINGS_DDL))

                target = sa.MetaData()
                async with engine.connect() as conn:
                    await conn.run_sync(lambda c: target.reflect(c))
                    target_revision = await conn.scalar(
                        sa.text("SELECT version_num FROM alembic_version")
                    )
                ordered = [
                    t.name for t in target.sorted_tables if t.name != "alembic_version"
                ]
                plans = _plan(src, source_tables, target, ordered, allow_drop)

                async with engine.connect() as conn:
                    trans = await conn.begin()
                    try:
                        # Some migrations seed singletons (e.g. routstr_fees id=1).
                        # The source rows are authoritative, so the seeds go.
                        replaced_seeds = []
                        for plan in reversed(plans):
                            if not plan.source_rows:
                                continue
                            seeded = await conn.execute(
                                sa.text(f'DELETE FROM "{plan.name}"')
                            )
                            if seeded.rowcount:
                                replaced_seeds.append(
                                    f"{plan.name} ({seeded.rowcount} rows)"
                                )
                        expected: dict[str, list[tuple[Any, ...]]] = {}
                        for plan in plans:
                            expected[plan.name] = await _copy_table(
                                conn, src, target.tables[plan.name], plan
                            )
                        sequences = await _reset_sequences(conn, target)
                        tables = await _verify(conn, src, plans, expected)
                        if args.dry_run:
                            await trans.rollback()
                        else:
                            await trans.commit()
                    except BaseException:
                        if trans.is_active:
                            await trans.rollback()
                        raise
            finally:
                await engine.dispose()
        finally:
            src.close()

    return {
        "status": "dry-run (rolled back)" if args.dry_run else "committed",
        "source": str(Path(args.source).resolve()),
        "source_alembic_revision": source_revision,
        "target": target_url.render_as_string(hide_password=True),
        "target_alembic_revision": target_revision,
        "replaced_migration_seed_rows": replaced_seeds,
        "sequences": sequences,
        "tables": tables,
        "reminder": (
            "keep ROUTSTR_SECRET_KEY / ROUTSTR_SECRET_KEY_FILE unchanged: encrypted "
            "secrets were copied verbatim"
        ),
    }


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
    os.environ.setdefault("UPSTREAM_BASE_URL", "http://migration.invalid")
    try:
        report = asyncio.run(migrate(args))
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
