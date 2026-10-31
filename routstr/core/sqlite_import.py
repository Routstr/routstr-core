"""Copy a Routstr SQLite application database into an empty PostgreSQL database.

Shared by ``scripts/migrate_sqlite_to_postgres.py`` and the node's startup import
(``SQLITE_IMPORT_PATH``). What a copy does, in order:

1. Takes a point-in-time snapshot of the source with SQLite's online backup API,
   so a running node cannot change rows mid-copy, and runs ``integrity_check``.
   A snapshot at an older known Alembic revision is upgraded to head; the
   original file is never written.
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

Encrypted secrets are copied verbatim, so the node must keep the same
``ROUTSTR_SECRET_KEY`` (or key file). The Cashu wallet stores under ``.wallet/``
stay SQLite and are not touched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from .db import DATABASE_URL
from .logging import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
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
IMPORT_MARKER_TABLE = "database_imports"
# Every worker reaches the startup import at once; one copies while the others
# queue here, then find the import marker and skip.
IMPORT_LOCK_KEY = 0x726F757473747223


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


def _sqlite_digest(db: Path) -> str:
    """Hash of every row in a SQLite file, to tell whether it changed since import."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        digest = hashlib.sha256()
        for name, columns in _sqlite_tables(conn).items():
            if name.startswith("sqlite_"):
                continue
            digest.update(f"{name}\n".encode())
            order = ", ".join(f'"{c}"' for c in columns)
            for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY {order}'):
                digest.update(repr(row).encode())
        return digest.hexdigest()
    finally:
        conn.close()


def _require_routstr_database(tables: dict[str, list[str]], source: Path) -> None:
    # An empty or foreign file would otherwise "import" nothing and be recorded
    # as done, so the real database put there later would be skipped.
    if "api_keys" not in tables:
        raise MigrationError(
            f"{source} is not a Routstr SQLite database (no api_keys table)"
        )


def _source_revision(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    # migrations/env.py otherwise targets the node's own DATABASE_URL.
    cfg.attributes["database_url"] = database_url
    return cfg


def _upgrade_schema(database_url: str) -> None:
    """Build the schema with the same Alembic chain the node runs at startup."""
    command.upgrade(_alembic_config(database_url), "head")


def _upgrade_snapshot(snapshot: Path, revision: str | None) -> str | None:
    """Validate the source revision and upgrade an older known snapshot to head.

    Copying old rows into a head schema would skip the data migrations in
    between. Unknown and unversioned files are refused before PostgreSQL is
    touched because their schema cannot be placed safely in this Alembic graph.
    """
    url = f"sqlite+aiosqlite:///{snapshot}"
    script = ScriptDirectory.from_config(_alembic_config(url))
    head = script.get_current_head()
    known = {rev.revision for rev in script.walk_revisions()}
    if revision is None:
        raise MigrationError(
            "the source SQLite database has no Alembic revision. Start a SQLite "
            "node of this version on the file once so it is stamped and upgraded, "
            "then retry the import."
        )
    if revision not in known:
        raise MigrationError(
            f"the source SQLite database has unknown Alembic revision {revision!r}; "
            "it comes from another or newer Routstr version. Run that version to "
            "perform the import, or upgrade this node first."
        )
    if revision == head:
        return None
    try:
        _upgrade_schema(url)
    except Exception as exc:
        raise MigrationError(
            f"upgrading a copy of the source from revision {revision} to "
            f"{head} failed ({exc}); the source file was not "
            "modified. Start a node on the SQLite database once to upgrade it in "
            "place, then retry the import."
        ) from exc
    return revision


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
    # SQLite keeps DATE as ISO text, which asyncpg will not bind to a DATE.
    if _is_type(ctype, sa.Date):
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise MigrationError(f"{where}: {value!r} is not a date") from None
    if _is_type(ctype, sa.LargeBinary):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        raise MigrationError(f"{where}: {value!r} is not binary data")
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


def _app_tables() -> list[str]:
    from sqlmodel import SQLModel

    from . import db  # noqa: F401  (registers every table)

    return [t.name for t in SQLModel.metadata.sorted_tables] + ["settings"]


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
        if max_id is None or max_id < 1:
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
            "fingerprint": source_fp,
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


def _source_fingerprint(tables: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for name in sorted(tables):
        digest.update(f"{name}:{tables[name]['fingerprint']}\n".encode())
    return digest.hexdigest()


async def copy_sqlite_to_postgres(
    source: Path,
    target: str,
    *,
    dry_run: bool = False,
    allow_drop: Iterable[str] = (),
    record_import: bool = False,
) -> dict[str, Any]:
    """Copy ``source`` into the empty PostgreSQL database ``target``; return a report.

    ``record_import`` writes the import marker in the same transaction as the
    rows, so a committed copy is never left without its record.
    """
    target_url = make_url(target)
    if target_url.get_backend_name() != "postgresql":
        raise MigrationError("--target must be a postgresql+asyncpg:// URL")
    if target_url.drivername != "postgresql+asyncpg":
        target_url = target_url.set(drivername="postgresql+asyncpg")
    target_dsn = target_url.render_as_string(hide_password=False)
    source = source.expanduser().resolve()

    with tempfile.TemporaryDirectory(prefix="routstr-pg-migrate-") as tmp:
        snapshot = _snapshot(source, Path(tmp))
        revision_conn = sqlite3.connect(snapshot)
        try:
            _require_routstr_database(_sqlite_tables(revision_conn), source)
            source_revision = _source_revision(revision_conn)
        finally:
            revision_conn.close()
        # Taken before the snapshot is upgraded, so it describes the file itself.
        source_digest = await asyncio.to_thread(_sqlite_digest, snapshot)
        upgraded_from = await asyncio.to_thread(
            _upgrade_snapshot, snapshot, source_revision
        )
        src = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        try:
            source_tables = _sqlite_tables(src)
            engine = create_async_engine(target_dsn, poolclass=NullPool)
            try:
                async with engine.connect() as conn:
                    occupied = await _target_has_rows(conn, _app_tables())
                if occupied:
                    raise MigrationError(
                        "target is not empty, refusing to merge into it: "
                        + ", ".join(occupied)
                    )

                await asyncio.to_thread(_upgrade_schema, target_dsn)
                async with engine.begin() as conn:
                    await conn.execute(sa.text(SETTINGS_DDL))

                target_meta = sa.MetaData()
                async with engine.connect() as conn:
                    await conn.run_sync(lambda c: target_meta.reflect(c))
                    target_revision = await conn.scalar(
                        sa.text("SELECT version_num FROM alembic_version")
                    )
                ordered = [
                    t.name
                    for t in target_meta.sorted_tables
                    if t.name != "alembic_version"
                ]
                plans = _plan(src, source_tables, target_meta, ordered, set(allow_drop))

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
                                conn, src, target_meta.tables[plan.name], plan
                            )
                        sequences = await _reset_sequences(conn, target_meta)
                        tables = await _verify(conn, src, plans, expected)
                        source_fingerprint = _source_fingerprint(tables)
                        if record_import:
                            await conn.execute(
                                target_meta.tables[IMPORT_MARKER_TABLE].insert(),
                                {
                                    "source_fingerprint": source_digest,
                                    "source_path": str(source),
                                    "source_revision": source_revision,
                                    "imported_at": int(time.time()),
                                },
                            )
                        if dry_run:
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
        "status": "dry-run (rolled back)" if dry_run else "committed",
        "source": str(source),
        "source_alembic_revision": source_revision,
        "source_upgraded_from_revision": upgraded_from,
        "source_fingerprint": source_fingerprint,
        "source_digest": source_digest,
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


async def _import_marker(conn: AsyncConnection) -> Any:
    has_marker_table = await conn.run_sync(
        lambda c: sa.inspect(c).has_table(IMPORT_MARKER_TABLE)
    )
    if not has_marker_table:
        return None
    result = await conn.execute(
        sa.text(
            f"SELECT source_path, source_fingerprint, imported_at "
            f"FROM {IMPORT_MARKER_TABLE} ORDER BY imported_at LIMIT 1"
        )
    )
    return result.first()


def _retire_source(source: Path) -> Path | None:
    """Rename an imported SQLite file (and its WAL sidecars) out of the way.

    A node still configured for the SQLite file then no longer finds the
    imported balances. Returns ``None`` when the file cannot be renamed (e.g. a
    read-only mount); the import marker alone still prevents a second import.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    retired = source.with_name(f"{source.name}.imported-{stamp}")
    try:
        source.rename(retired)
    except OSError as exc:
        logger.warning(
            "Imported SQLite database could not be renamed; remove or archive it "
            "so no node keeps using it",
            extra={"source": str(source), "error": str(exc)},
        )
        return None
    # The main file is renamed first: a WAL left behind without it is harmless,
    # a main file separated from its WAL would lose the newest commits.
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            try:
                sidecar.rename(retired.with_name(retired.name + suffix))
            except OSError as exc:
                logger.warning(
                    "SQLite sidecar could not be renamed with its database",
                    extra={"sidecar": str(sidecar), "error": str(exc)},
                )
    return retired


async def _changed_since_import(source: Path, imported_digest: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="routstr-pg-migrate-") as tmp:
        snapshot = _snapshot(source, Path(tmp))
        return await asyncio.to_thread(_sqlite_digest, snapshot) != imported_digest


async def _import_once(engine: AsyncEngine, database_url: str, source: Path) -> None:
    async with engine.connect() as conn:
        marker = await _import_marker(conn)
        occupied = [] if marker else await _target_has_rows(conn, _app_tables())
    if marker is not None:
        if source.exists() and await _changed_since_import(
            source, marker.source_fingerprint
        ):
            raise MigrationError(
                f"{source} changed after it was imported into PostgreSQL, so a "
                "SQLite node has been using it. Its new rows are not in "
                "PostgreSQL; reconcile them by hand, then move the file away."
            )
        logger.info(
            "SQLite import already done; skipping",
            extra={
                "imported_from": marker.source_path,
                "source_fingerprint": marker.source_fingerprint,
                "imported_at": marker.imported_at,
            },
        )
        if source.exists():
            logger.warning(
                "SQLITE_IMPORT_PATH still points at the file that was already "
                "imported; move it away so no SQLite node keeps using it",
                extra={"source": str(source)},
            )
        return
    if not source.exists():
        logger.warning(
            "SQLITE_IMPORT_PATH does not exist, so nothing is imported. If this "
            "node should take over an existing SQLite database, stop it and fix "
            "the path now: once this database holds data, no import is accepted.",
            extra={"source": str(source)},
        )
        return
    if occupied:
        raise MigrationError(
            "DATABASE_URL already holds data that was not imported from "
            "SQLITE_IMPORT_PATH; refusing to merge into it ("
            + ", ".join(occupied)
            + "). Set SQLITE_IMPORT_PATH to an empty value to keep using this "
            "database, or point DATABASE_URL at an empty one."
        )

    report = await copy_sqlite_to_postgres(source, database_url, record_import=True)
    retired = _retire_source(source)
    logger.info(
        "SQLite database imported into PostgreSQL",
        extra={
            "source": str(source),
            "renamed_to": str(retired) if retired else None,
            "source_fingerprint": report["source_fingerprint"],
            "source_upgraded_from_revision": report["source_upgraded_from_revision"],
            "rows": {name: t["rows"]["target"] for name, t in report["tables"].items()},
        },
    )


async def import_sqlite_on_startup(database_url: str = DATABASE_URL) -> None:
    """Import ``SQLITE_IMPORT_PATH`` into an empty PostgreSQL ``DATABASE_URL`` once.

    No-op unless both are set. Any failure leaves PostgreSQL without the
    imported rows and is raised, so the node does not start.
    """
    source_setting = os.environ.get("SQLITE_IMPORT_PATH", "")
    if not source_setting:
        return
    if make_url(database_url).get_backend_name() != "postgresql":
        logger.warning(
            "SQLITE_IMPORT_PATH is ignored because DATABASE_URL is not PostgreSQL"
        )
        return
    source = Path(source_setting).expanduser().resolve()

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as lock_conn:
            await lock_conn.execute(
                sa.text("SELECT pg_advisory_lock(:key)"), {"key": IMPORT_LOCK_KEY}
            )
            await lock_conn.commit()
            try:
                await _import_once(engine, database_url, source)
            except Exception as exc:
                logger.error(
                    "SQLite import did not run; nothing was copied and the node "
                    "will not start. Fix the cause and restart, or set "
                    "SQLITE_IMPORT_PATH to an empty value to start without "
                    "importing.",
                    extra={
                        "source": str(source),
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
                raise
            finally:
                # Closing the connection releases the lock too, so a failed
                # unlock must not replace the real error or fail a good import.
                try:
                    await lock_conn.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)"),
                        {"key": IMPORT_LOCK_KEY},
                    )
                    await lock_conn.commit()
                except Exception:
                    logger.warning(
                        "SQLite import lock could not be released explicitly",
                        exc_info=True,
                    )
    finally:
        await engine.dispose()
