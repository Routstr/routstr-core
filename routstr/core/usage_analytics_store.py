import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .logging import get_logger

logger = get_logger(__name__)


class UsageAnalyticsStore:
    """
    Incremental usage analytics index backed by SQLite.

    Instead of rescanning raw JSON log files for every dashboard request, we keep
    a rolling minute-level aggregate that is updated from only newly appended log
    bytes.
    """

    SCHEMA_VERSION = "4"

    def __init__(self, logs_dir: Path, db_path: Path | None = None):
        self.logs_dir = logs_dir
        self.db_path = db_path or (logs_dir / "usage_analytics.db")
        self._lock = Lock()
        self._conn: sqlite3.Connection | None = None

    def get_dashboard(
        self,
        *,
        interval_minutes: int,
        hours_back: int,
        error_limit: int,
        model_limit: int,
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._get_connection_locked()
            self._ensure_up_to_date_locked(conn)
            cutoff_timestamp = self._cutoff_timestamp(hours_back)

            summary = self._query_summary_locked(conn, cutoff_timestamp)
            metrics = self._query_metrics_locked(
                conn,
                cutoff_timestamp=cutoff_timestamp,
                interval_minutes=interval_minutes,
                hours_back=hours_back,
            )
            error_details = self._query_error_details_locked(
                conn,
                cutoff_timestamp=cutoff_timestamp,
                limit=error_limit,
                total_error_count=summary["total_errors"],
            )
            revenue_by_model = self._query_revenue_by_model_locked(
                conn,
                cutoff_timestamp=cutoff_timestamp,
                limit=model_limit,
            )
            model_usage_mix = self._query_model_usage_mix_locked(
                conn,
                cutoff_timestamp=cutoff_timestamp,
                interval_minutes=interval_minutes,
                hours_back=hours_back,
                limit=model_limit,
            )

            return {
                "metrics": metrics,
                "summary": summary,
                "error_details": error_details,
                "revenue_by_model": revenue_by_model,
                "model_usage_mix": model_usage_mix,
            }

    def get_summary(self, *, hours_back: int) -> dict[str, Any]:
        with self._lock:
            conn = self._get_connection_locked()
            self._ensure_up_to_date_locked(conn)
            cutoff_timestamp = self._cutoff_timestamp(hours_back)
            return self._query_summary_locked(conn, cutoff_timestamp)

    def get_metrics(
        self,
        *,
        interval_minutes: int,
        hours_back: int,
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._get_connection_locked()
            self._ensure_up_to_date_locked(conn)
            cutoff_timestamp = self._cutoff_timestamp(hours_back)
            return self._query_metrics_locked(
                conn,
                cutoff_timestamp=cutoff_timestamp,
                interval_minutes=interval_minutes,
                hours_back=hours_back,
            )

    def get_error_details(self, *, hours_back: int, limit: int) -> dict[str, Any]:
        with self._lock:
            conn = self._get_connection_locked()
            self._ensure_up_to_date_locked(conn)
            cutoff_timestamp = self._cutoff_timestamp(hours_back)
            return self._query_error_details_locked(
                conn,
                cutoff_timestamp=cutoff_timestamp,
                limit=limit,
            )

    def get_revenue_by_model(self, *, hours_back: int, limit: int) -> dict[str, Any]:
        with self._lock:
            conn = self._get_connection_locked()
            self._ensure_up_to_date_locked(conn)
            cutoff_timestamp = self._cutoff_timestamp(hours_back)
            return self._query_revenue_by_model_locked(
                conn,
                cutoff_timestamp=cutoff_timestamp,
                limit=limit,
            )

    def _get_connection_locked(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-20000")
        self._initialize_schema_locked(conn)
        self._conn = conn
        return conn

    def _initialize_schema_locked(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        current_version_row = conn.execute(
            "SELECT value FROM analytics_meta WHERE key = 'schema_version'"
        ).fetchone()
        current_version = current_version_row[0] if current_version_row else None

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics_file_state (
                path TEXT PRIMARY KEY,
                inode INTEGER NOT NULL,
                offset INTEGER NOT NULL,
                size INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics_minute (
                minute_ts TEXT PRIMARY KEY,
                total_entries INTEGER NOT NULL DEFAULT 0,
                total_requests INTEGER NOT NULL DEFAULT 0,
                successful_chat_completions INTEGER NOT NULL DEFAULT 0,
                failed_requests INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                warnings INTEGER NOT NULL DEFAULT 0,
                payment_processed INTEGER NOT NULL DEFAULT 0,
                upstream_errors INTEGER NOT NULL DEFAULT 0,
                revenue_msats REAL NOT NULL DEFAULT 0,
                refunds_msats REAL NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics_model_minute (
                minute_ts TEXT NOT NULL,
                model TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                successful INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                revenue_msats REAL NOT NULL DEFAULT 0,
                refunds_msats REAL NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (minute_ts, model)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics_model_presence_minute (
                minute_ts TEXT NOT NULL,
                model TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (minute_ts, model)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics_error_type_minute (
                minute_ts TEXT NOT NULL,
                error_type TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (minute_ts, error_type)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analytics_error_events (
                timestamp TEXT NOT NULL,
                message TEXT NOT NULL,
                error_type TEXT NOT NULL,
                pathname TEXT NOT NULL,
                lineno INTEGER NOT NULL,
                request_id TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_model_minute_ts ON analytics_model_minute (minute_ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_model_minute_model_ts ON analytics_model_minute (model, minute_ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_model_presence_ts ON analytics_model_presence_minute (minute_ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_error_type_minute_ts ON analytics_error_type_minute (minute_ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_error_events_ts ON analytics_error_events (timestamp DESC)"
        )
        self._migrate_schema_locked(conn)
        if current_version != self.SCHEMA_VERSION:
            conn.execute(
                """
                INSERT OR REPLACE INTO analytics_meta (key, value)
                VALUES ('schema_version', ?)
                """,
                (self.SCHEMA_VERSION,),
            )
        conn.commit()

    def _migrate_schema_locked(self, conn: sqlite3.Connection) -> None:
        self._ensure_column_locked(
            conn,
            "analytics_minute",
            "input_tokens",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column_locked(
            conn,
            "analytics_minute",
            "output_tokens",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column_locked(
            conn,
            "analytics_minute",
            "total_tokens",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column_locked(
            conn,
            "analytics_model_minute",
            "input_tokens",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column_locked(
            conn,
            "analytics_model_minute",
            "output_tokens",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column_locked(
            conn,
            "analytics_model_minute",
            "total_tokens",
            "INTEGER NOT NULL DEFAULT 0",
        )

    def _ensure_column_locked(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        column_definition: str,
    ) -> None:
        existing_columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in existing_columns:
            return

        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {column_definition}"
        )
        logger.info(f"Migrated analytics schema: added {table}.{column}")

    def _drop_index_tables_locked(self, conn: sqlite3.Connection) -> None:
        conn.execute("DROP TABLE IF EXISTS analytics_file_state")
        conn.execute("DROP TABLE IF EXISTS analytics_minute")
        conn.execute("DROP TABLE IF EXISTS analytics_model_minute")
        conn.execute("DROP TABLE IF EXISTS analytics_model_presence_minute")
        conn.execute("DROP TABLE IF EXISTS analytics_error_type_minute")
        conn.execute("DROP TABLE IF EXISTS analytics_error_events")

    def _ensure_up_to_date_locked(self, conn: sqlite3.Connection) -> None:
        if not self.logs_dir.exists():
            return

        log_files = sorted(self.logs_dir.glob("app_*.log"))
        if not log_files:
            return

        requires_rebuild = False

        for log_file in log_files:
            try:
                self._process_log_file_locked(conn, log_file)
            except RuntimeError:
                requires_rebuild = True
                break
            except Exception as exc:
                logger.error(f"Failed indexing usage analytics for {log_file}: {exc}")
                continue

        if requires_rebuild:
            logger.warning(
                "Usage analytics index out-of-sync, rebuilding from all log files"
            )
            self._rebuild_locked(conn, log_files)
            return

        # Commit even when only file-state metadata changed
        # (for example when we intentionally keep offset at the last full line).
        conn.commit()

    def _rebuild_locked(
        self, conn: sqlite3.Connection, log_files: list[Path] | None = None
    ) -> None:
        self._drop_index_tables_locked(conn)
        self._initialize_schema_locked(conn)

        files = log_files if log_files is not None else sorted(self.logs_dir.glob("app_*.log"))
        for log_file in files:
            try:
                self._process_log_file_locked(conn, log_file, force_full_read=True)
            except Exception as exc:
                logger.error(f"Failed rebuilding usage analytics for {log_file}: {exc}")
        conn.commit()

    def _process_log_file_locked(
        self,
        conn: sqlite3.Connection,
        log_file: Path,
        force_full_read: bool = False,
    ) -> bool:
        stat = log_file.stat()
        inode = int(getattr(stat, "st_ino", 0))
        file_size = int(stat.st_size)
        log_file_path = str(log_file.resolve())

        previous_offset = 0
        if not force_full_read:
            row = conn.execute(
                """
                SELECT inode, offset
                FROM analytics_file_state
                WHERE path = ?
                """,
                (log_file_path,),
            ).fetchone()
            if row is not None:
                previous_inode = int(row["inode"])
                previous_offset = int(row["offset"])
                if previous_inode and inode and previous_inode != inode:
                    raise RuntimeError("inode changed")
                if previous_offset > file_size:
                    raise RuntimeError("file shrunk")

        if previous_offset >= file_size and not force_full_read:
            self._upsert_file_state_locked(
                conn,
                path=log_file_path,
                inode=inode,
                offset=file_size,
                size=file_size,
            )
            return False

        (
            end_offset,
            minute_updates,
            model_updates,
            model_presence_updates,
            error_type_updates,
            error_events,
        ) = self._collect_updates_from_file(log_file, previous_offset)

        self._apply_updates_locked(
            conn=conn,
            minute_updates=minute_updates,
            model_updates=model_updates,
            model_presence_updates=model_presence_updates,
            error_type_updates=error_type_updates,
            error_events=error_events,
        )

        latest_size = int(log_file.stat().st_size)
        self._upsert_file_state_locked(
            conn,
            path=log_file_path,
            inode=inode,
            offset=end_offset,
            size=latest_size,
        )
        return end_offset != previous_offset

    def _upsert_file_state_locked(
        self,
        conn: sqlite3.Connection,
        *,
        path: str,
        inode: int,
        offset: int,
        size: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO analytics_file_state (path, inode, offset, size, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                inode = excluded.inode,
                offset = excluded.offset,
                size = excluded.size,
                updated_at = excluded.updated_at
            """,
            (path, inode, offset, size, time.time()),
        )

    def _collect_updates_from_file(
        self, log_file: Path, start_offset: int
    ) -> tuple[
        int,
        dict[str, dict[str, float]],
        dict[tuple[str, str], dict[str, float]],
        dict[tuple[str, str], int],
        dict[tuple[str, str], int],
        list[tuple[str, str, str, str, int, str]],
    ]:
        minute_updates: dict[str, dict[str, float]] = defaultdict(
            self._new_minute_stats
        )
        model_updates: dict[tuple[str, str], dict[str, float]] = defaultdict(
            self._new_model_stats
        )
        model_presence_updates: dict[tuple[str, str], int] = defaultdict(int)
        error_type_updates: dict[tuple[str, str], int] = defaultdict(int)
        error_events: list[tuple[str, str, str, str, int, str]] = []

        end_offset = start_offset
        with open(log_file, "rb") as f:
            f.seek(start_offset)

            while True:
                line_start = f.tell()
                raw_line = f.readline()
                if not raw_line:
                    break

                # If the writer is appending and we catch a partial line at EOF,
                # do not advance beyond it. We'll parse it on the next refresh.
                if not raw_line.endswith(b"\n"):
                    f.seek(line_start)
                    break

                end_offset = f.tell()
                if not raw_line.strip():
                    continue

                try:
                    entry = json.loads(raw_line)
                except Exception:
                    continue

                if not isinstance(entry, dict):
                    continue

                minute_key = self._minute_key(entry.get("asctime"))
                if minute_key is None:
                    continue

                bucket = minute_updates[minute_key]
                bucket["total_entries"] += 1

                message_value = entry.get("message", "")
                message = str(message_value).lower()
                level = str(entry.get("levelname", "")).upper()

                model_raw = entry.get("model", "unknown")
                model = model_raw if isinstance(model_raw, str) else "unknown"

                if level == "ERROR":
                    bucket["errors"] += 1
                    error_type = str(entry.get("error_type", "unknown"))
                    error_type_updates[(minute_key, error_type)] += 1

                    lineno_value = entry.get("lineno", 0)
                    try:
                        lineno = int(lineno_value)
                    except (TypeError, ValueError):
                        lineno = 0

                    error_events.append(
                        (
                            str(entry.get("asctime", "")),
                            str(message_value),
                            error_type,
                            str(entry.get("pathname", "")),
                            lineno,
                            str(entry.get("request_id", "")),
                        )
                    )
                elif level == "WARNING":
                    bucket["warnings"] += 1

                completed, revenue_msats, input_tokens, output_tokens = (
                    self._extract_success_metrics(entry, message)
                )
                if completed:
                    bucket["total_requests"] += 1
                    bucket["successful_chat_completions"] += 1
                    model_bucket = model_updates[(minute_key, model)]
                    model_bucket["requests"] += 1
                    model_bucket["successful"] += 1
                    bucket["input_tokens"] += input_tokens
                    bucket["output_tokens"] += output_tokens
                    bucket["total_tokens"] += input_tokens + output_tokens
                    model_bucket["input_tokens"] += input_tokens
                    model_bucket["output_tokens"] += output_tokens
                    model_bucket["total_tokens"] += input_tokens + output_tokens

                    if revenue_msats > 0:
                        bucket["revenue_msats"] += revenue_msats
                        model_bucket["revenue_msats"] += revenue_msats

                failed = (
                    "upstream request failed" in message
                    or "revert payment" in message
                )
                if failed:
                    bucket["total_requests"] += 1
                    bucket["failed_requests"] += 1
                    model_bucket = model_updates[(minute_key, model)]
                    model_bucket["requests"] += 1
                    model_bucket["failed"] += 1

                if "payment processed successfully" in message:
                    bucket["payment_processed"] += 1

                if level == "ERROR" and "upstream" in message:
                    bucket["upstream_errors"] += 1

                if model != "unknown":
                    model_presence_updates[(minute_key, model)] += 1

                if "revert payment" in message:
                    max_cost = entry.get("max_cost_for_model", 0)
                    if isinstance(max_cost, (int, float)) and max_cost > 0:
                        max_cost_float = float(max_cost)
                        bucket["refunds_msats"] += max_cost_float
                        model_updates[(minute_key, model)][
                            "refunds_msats"
                        ] += max_cost_float

        return (
            end_offset,
            minute_updates,
            model_updates,
            model_presence_updates,
            error_type_updates,
            error_events,
        )

    def _apply_updates_locked(
        self,
        *,
        conn: sqlite3.Connection,
        minute_updates: dict[str, dict[str, float]],
        model_updates: dict[tuple[str, str], dict[str, float]],
        model_presence_updates: dict[tuple[str, str], int],
        error_type_updates: dict[tuple[str, str], int],
        error_events: list[tuple[str, str, str, str, int, str]],
    ) -> None:
        if minute_updates:
            rows = [
                (
                    minute_ts,
                    int(stats["total_entries"]),
                    int(stats["total_requests"]),
                    int(stats["successful_chat_completions"]),
                    int(stats["failed_requests"]),
                    int(stats["errors"]),
                    int(stats["warnings"]),
                    int(stats["payment_processed"]),
                    int(stats["upstream_errors"]),
                    float(stats["revenue_msats"]),
                    float(stats["refunds_msats"]),
                    int(stats["input_tokens"]),
                    int(stats["output_tokens"]),
                    int(stats["total_tokens"]),
                )
                for minute_ts, stats in minute_updates.items()
            ]
            conn.executemany(
                """
                INSERT INTO analytics_minute (
                    minute_ts,
                    total_entries,
                    total_requests,
                    successful_chat_completions,
                    failed_requests,
                    errors,
                    warnings,
                    payment_processed,
                    upstream_errors,
                    revenue_msats,
                    refunds_msats,
                    input_tokens,
                    output_tokens,
                    total_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(minute_ts) DO UPDATE SET
                    total_entries = total_entries + excluded.total_entries,
                    total_requests = total_requests + excluded.total_requests,
                    successful_chat_completions = successful_chat_completions + excluded.successful_chat_completions,
                    failed_requests = failed_requests + excluded.failed_requests,
                    errors = errors + excluded.errors,
                    warnings = warnings + excluded.warnings,
                    payment_processed = payment_processed + excluded.payment_processed,
                    upstream_errors = upstream_errors + excluded.upstream_errors,
                    revenue_msats = revenue_msats + excluded.revenue_msats,
                    refunds_msats = refunds_msats + excluded.refunds_msats,
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    total_tokens = total_tokens + excluded.total_tokens
                """,
                rows,
            )

        if model_updates:
            rows = [
                (
                    minute_ts,
                    model,
                    int(stats["requests"]),
                    int(stats["successful"]),
                    int(stats["failed"]),
                    float(stats["revenue_msats"]),
                    float(stats["refunds_msats"]),
                    int(stats["input_tokens"]),
                    int(stats["output_tokens"]),
                    int(stats["total_tokens"]),
                )
                for (minute_ts, model), stats in model_updates.items()
            ]
            conn.executemany(
                """
                INSERT INTO analytics_model_minute (
                    minute_ts,
                    model,
                    requests,
                    successful,
                    failed,
                    revenue_msats,
                    refunds_msats,
                    input_tokens,
                    output_tokens,
                    total_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(minute_ts, model) DO UPDATE SET
                    requests = requests + excluded.requests,
                    successful = successful + excluded.successful,
                    failed = failed + excluded.failed,
                    revenue_msats = revenue_msats + excluded.revenue_msats,
                    refunds_msats = refunds_msats + excluded.refunds_msats,
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    total_tokens = total_tokens + excluded.total_tokens
                """,
                rows,
            )

        if model_presence_updates:
            rows = [
                (minute_ts, model, count)
                for (minute_ts, model), count in model_presence_updates.items()
            ]
            conn.executemany(
                """
                INSERT INTO analytics_model_presence_minute (
                    minute_ts,
                    model,
                    count
                )
                VALUES (?, ?, ?)
                ON CONFLICT(minute_ts, model) DO UPDATE SET
                    count = count + excluded.count
                """,
                rows,
            )

        if error_type_updates:
            rows = [
                (minute_ts, error_type, count)
                for (minute_ts, error_type), count in error_type_updates.items()
            ]
            conn.executemany(
                """
                INSERT INTO analytics_error_type_minute (
                    minute_ts,
                    error_type,
                    count
                )
                VALUES (?, ?, ?)
                ON CONFLICT(minute_ts, error_type) DO UPDATE SET
                    count = count + excluded.count
                """,
                rows,
            )

        if error_events:
            conn.executemany(
                """
                INSERT INTO analytics_error_events (
                    timestamp,
                    message,
                    error_type,
                    pathname,
                    lineno,
                    request_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                error_events,
            )

    def _query_metrics_locked(
        self,
        conn: sqlite3.Connection,
        *,
        cutoff_timestamp: str,
        interval_minutes: int,
        hours_back: int,
    ) -> dict[str, Any]:
        bucket_seconds = max(60, int(interval_minutes) * 60)
        rows = conn.execute(
            """
            SELECT
                datetime(
                    (CAST(strftime('%s', minute_ts) AS INTEGER) / ?) * ?,
                    'unixepoch'
                ) AS bucket_ts,
                COALESCE(SUM(total_requests), 0) AS total_requests,
                COALESCE(SUM(successful_chat_completions), 0) AS successful_chat_completions,
                COALESCE(SUM(failed_requests), 0) AS failed_requests,
                COALESCE(SUM(errors), 0) AS errors,
                COALESCE(SUM(warnings), 0) AS warnings,
                COALESCE(SUM(payment_processed), 0) AS payment_processed,
                COALESCE(SUM(upstream_errors), 0) AS upstream_errors,
                COALESCE(SUM(revenue_msats), 0) AS revenue_msats,
                COALESCE(SUM(refunds_msats), 0) AS refunds_msats,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM analytics_minute
            WHERE minute_ts >= ?
            GROUP BY bucket_ts
            ORDER BY bucket_ts
            """,
            (bucket_seconds, bucket_seconds, cutoff_timestamp),
        ).fetchall()

        totals: dict[str, float] = {
            "total_requests": 0.0,
            "successful_chat_completions": 0.0,
            "failed_requests": 0.0,
            "errors": 0.0,
            "warnings": 0.0,
            "payment_processed": 0.0,
            "upstream_errors": 0.0,
            "revenue_msats": 0.0,
            "refunds_msats": 0.0,
            "input_tokens": 0.0,
            "output_tokens": 0.0,
            "total_tokens": 0.0,
        }

        points: list[dict[str, Any]] = []
        for row in rows:
            total_requests = int(row["total_requests"])
            successful = int(row["successful_chat_completions"])
            failed = int(row["failed_requests"])
            errors = int(row["errors"])
            warnings = int(row["warnings"])
            payment_processed = int(row["payment_processed"])
            upstream_errors = int(row["upstream_errors"])
            revenue_msats = float(row["revenue_msats"])
            refunds_msats = float(row["refunds_msats"])
            input_tokens = int(row["input_tokens"])
            output_tokens = int(row["output_tokens"])
            total_tokens = int(row["total_tokens"])

            totals["total_requests"] += total_requests
            totals["successful_chat_completions"] += successful
            totals["failed_requests"] += failed
            totals["errors"] += errors
            totals["warnings"] += warnings
            totals["payment_processed"] += payment_processed
            totals["upstream_errors"] += upstream_errors
            totals["revenue_msats"] += revenue_msats
            totals["refunds_msats"] += refunds_msats
            totals["input_tokens"] += input_tokens
            totals["output_tokens"] += output_tokens
            totals["total_tokens"] += total_tokens

            points.append(
                {
                    "timestamp": str(row["bucket_ts"]),
                    "total_requests": total_requests,
                    "successful_chat_completions": successful,
                    "failed_requests": failed,
                    "errors": errors,
                    "warnings": warnings,
                    "payment_processed": payment_processed,
                    "upstream_errors": upstream_errors,
                    "revenue_msats": revenue_msats,
                    "refunds_msats": refunds_msats,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "requests": total_requests,
                }
            )

        normalized_totals: dict[str, int | float] = {
            "total_requests": int(totals["total_requests"]),
            "successful_chat_completions": int(totals["successful_chat_completions"]),
            "failed_requests": int(totals["failed_requests"]),
            "errors": int(totals["errors"]),
            "warnings": int(totals["warnings"]),
            "payment_processed": int(totals["payment_processed"]),
            "upstream_errors": int(totals["upstream_errors"]),
            "revenue_msats": float(totals["revenue_msats"]),
            "refunds_msats": float(totals["refunds_msats"]),
            "input_tokens": int(totals["input_tokens"]),
            "output_tokens": int(totals["output_tokens"]),
            "total_tokens": int(totals["total_tokens"]),
        }

        return {
            "metrics": points,
            "interval_minutes": interval_minutes,
            "hours_back": hours_back,
            "total_buckets": len(points),
            "totals": normalized_totals,
        }

    def _query_summary_locked(
        self, conn: sqlite3.Connection, cutoff_timestamp: str
    ) -> dict[str, Any]:
        totals = conn.execute(
            """
            SELECT
                COALESCE(SUM(total_entries), 0) AS total_entries,
                COALESCE(SUM(total_requests), 0) AS total_requests,
                COALESCE(SUM(successful_chat_completions), 0) AS successful_chat_completions,
                COALESCE(SUM(failed_requests), 0) AS failed_requests,
                COALESCE(SUM(errors), 0) AS total_errors,
                COALESCE(SUM(warnings), 0) AS total_warnings,
                COALESCE(SUM(payment_processed), 0) AS payment_processed,
                COALESCE(SUM(upstream_errors), 0) AS upstream_errors,
                COALESCE(SUM(revenue_msats), 0) AS revenue_msats,
                COALESCE(SUM(refunds_msats), 0) AS refunds_msats,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM analytics_minute
            WHERE minute_ts >= ?
            """,
            (cutoff_timestamp,),
        ).fetchone()

        unique_models = [
            str(row[0])
            for row in conn.execute(
                """
                SELECT DISTINCT model
                FROM analytics_model_presence_minute
                WHERE minute_ts >= ?
                ORDER BY model ASC
                """,
                (cutoff_timestamp,),
            ).fetchall()
        ]

        error_types = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                """
                SELECT error_type, COALESCE(SUM(count), 0) AS total_count
                FROM analytics_error_type_minute
                WHERE minute_ts >= ?
                GROUP BY error_type
                """,
                (cutoff_timestamp,),
            ).fetchall()
        }

        total_requests = int(totals["total_requests"])
        successful = int(totals["successful_chat_completions"])
        failed_requests = int(totals["failed_requests"])
        input_tokens = int(totals["input_tokens"])
        output_tokens = int(totals["output_tokens"])
        total_tokens = int(totals["total_tokens"])

        revenue_msats = float(totals["revenue_msats"])
        refunds_msats = float(totals["refunds_msats"])
        net_revenue_msats = revenue_msats - refunds_msats

        revenue_sats = revenue_msats / 1000
        refunds_sats = refunds_msats / 1000
        net_revenue_sats = net_revenue_msats / 1000

        return {
            "total_entries": int(totals["total_entries"]),
            "total_requests": total_requests,
            "successful_chat_completions": successful,
            "failed_requests": failed_requests,
            "total_errors": int(totals["total_errors"]),
            "total_warnings": int(totals["total_warnings"]),
            "payment_processed": int(totals["payment_processed"]),
            "upstream_errors": int(totals["upstream_errors"]),
            "unique_models_count": len(unique_models),
            "unique_models": unique_models,
            "error_types": error_types,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "avg_input_tokens_per_completion": (input_tokens / successful)
            if successful > 0
            else 0,
            "avg_output_tokens_per_completion": (output_tokens / successful)
            if successful > 0
            else 0,
            "avg_total_tokens_per_completion": (total_tokens / successful)
            if successful > 0
            else 0,
            "success_rate": (successful / total_requests * 100)
            if total_requests > 0
            else 0,
            "revenue_msats": revenue_msats,
            "refunds_msats": refunds_msats,
            "revenue_sats": revenue_sats,
            "refunds_sats": refunds_sats,
            "net_revenue_msats": net_revenue_msats,
            "net_revenue_sats": net_revenue_sats,
            "avg_revenue_per_request_msats": (revenue_msats / successful)
            if successful > 0
            else 0,
            "refund_rate": (failed_requests / total_requests * 100)
            if total_requests > 0
            else 0,
        }

    def _query_error_details_locked(
        self,
        conn: sqlite3.Connection,
        *,
        cutoff_timestamp: str,
        limit: int,
        total_error_count: int | None = None,
    ) -> dict[str, Any]:
        rows = conn.execute(
            """
            SELECT
                timestamp,
                message,
                error_type,
                pathname,
                lineno,
                request_id
            FROM analytics_error_events
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (cutoff_timestamp, limit),
        ).fetchall()

        if total_error_count is None:
            total_error_count_row = conn.execute(
                """
                SELECT COALESCE(SUM(errors), 0)
                FROM analytics_minute
                WHERE minute_ts >= ?
                """,
                (cutoff_timestamp,),
            ).fetchone()
            total_error_count = int(total_error_count_row[0]) if total_error_count_row else 0

        return {
            "errors": [
                {
                    "timestamp": str(row["timestamp"]),
                    "message": str(row["message"]),
                    "error_type": str(row["error_type"]),
                    "pathname": str(row["pathname"]),
                    "lineno": int(row["lineno"]),
                    "request_id": str(row["request_id"]),
                }
                for row in rows
            ],
            "total_count": int(total_error_count),
        }

    def _query_revenue_by_model_locked(
        self,
        conn: sqlite3.Connection,
        *,
        cutoff_timestamp: str,
        limit: int,
    ) -> dict[str, Any]:
        rows = conn.execute(
            """
            SELECT
                model,
                COALESCE(SUM(revenue_msats), 0) AS revenue_msats,
                COALESCE(SUM(refunds_msats), 0) AS refunds_msats,
                COALESCE(SUM(requests), 0) AS requests,
                COALESCE(SUM(successful), 0) AS successful,
                COALESCE(SUM(failed), 0) AS failed
            FROM analytics_model_minute
            WHERE minute_ts >= ?
            GROUP BY model
            ORDER BY (COALESCE(SUM(revenue_msats), 0) - COALESCE(SUM(refunds_msats), 0)) DESC
            """,
            (cutoff_timestamp,),
        ).fetchall()

        models: list[dict[str, Any]] = []
        total_revenue_sats = 0.0

        for row in rows:
            revenue_msats = float(row["revenue_msats"])
            refunds_msats = float(row["refunds_msats"])
            revenue_sats = revenue_msats / 1000
            refunds_sats = refunds_msats / 1000
            net_revenue_sats = revenue_sats - refunds_sats
            successful = int(row["successful"])

            models.append(
                {
                    "model": str(row["model"]),
                    "revenue_sats": revenue_sats,
                    "refunds_sats": refunds_sats,
                    "net_revenue_sats": net_revenue_sats,
                    "requests": int(row["requests"]),
                    "successful": successful,
                    "failed": int(row["failed"]),
                    "avg_revenue_per_request": (revenue_sats / successful)
                    if successful > 0
                    else 0,
                }
            )
            total_revenue_sats += net_revenue_sats

        return {
            "models": models[:limit],
            "total_revenue_sats": total_revenue_sats,
            "total_models": len(models),
        }

    def _query_model_usage_mix_locked(
        self,
        conn: sqlite3.Connection,
        *,
        cutoff_timestamp: str,
        interval_minutes: int,
        hours_back: int,
        limit: int,
    ) -> dict[str, Any]:
        top_limit = max(1, min(int(limit), 20))
        top_rows = conn.execute(
            """
            SELECT
                model,
                COALESCE(SUM(successful), 0) AS total_successful
            FROM analytics_model_minute
            WHERE minute_ts >= ?
              AND model != 'unknown'
            GROUP BY model
            ORDER BY total_successful DESC
            LIMIT ?
            """,
            (cutoff_timestamp, top_limit),
        ).fetchall()

        top_models = [
            str(row["model"])
            for row in top_rows
            if int(row["total_successful"] or 0) > 0
        ]

        bucket_seconds = max(60, int(interval_minutes) * 60)
        total_rows = conn.execute(
            """
            SELECT
                datetime(
                    (CAST(strftime('%s', minute_ts) AS INTEGER) / ?) * ?,
                    'unixepoch'
                ) AS bucket_ts,
                COALESCE(SUM(successful), 0) AS total_successful,
                COALESCE(SUM(revenue_msats), 0) AS total_revenue_msats,
                COALESCE(SUM(total_tokens), 0) AS total_tokens
            FROM analytics_model_minute
            WHERE minute_ts >= ?
            GROUP BY bucket_ts
            ORDER BY bucket_ts
            """,
            (bucket_seconds, bucket_seconds, cutoff_timestamp),
        ).fetchall()

        bucket_index: dict[str, dict[str, Any]] = {}
        for row in total_rows:
            total_successful = int(row["total_successful"])
            total_revenue_msats = float(row["total_revenue_msats"])
            total_tokens = int(row["total_tokens"])
            if (
                total_successful <= 0
                and total_revenue_msats <= 0
                and total_tokens <= 0
            ):
                continue

            bucket_ts = str(row["bucket_ts"])
            bucket = bucket_index.setdefault(
                bucket_ts,
                {
                    "timestamp": bucket_ts,
                    "total_successful": 0,
                    "total_revenue_msats": 0.0,
                    "total_tokens": 0,
                    "others": 0,
                    "others_revenue_msats": 0.0,
                    "others_tokens": 0,
                    "model_counts": {},
                    "model_revenue_msats": {},
                    "model_tokens": {},
                },
            )
            bucket["total_successful"] = total_successful
            bucket["total_revenue_msats"] = total_revenue_msats
            bucket["total_tokens"] = total_tokens
            bucket["others"] = total_successful
            bucket["others_revenue_msats"] = total_revenue_msats
            bucket["others_tokens"] = total_tokens

        if top_models and bucket_index:
            placeholders = ",".join("?" for _ in top_models)
            top_model_rows = conn.execute(
                f"""
                SELECT
                    datetime(
                        (CAST(strftime('%s', minute_ts) AS INTEGER) / ?) * ?,
                        'unixepoch'
                    ) AS bucket_ts,
                    model,
                    COALESCE(SUM(successful), 0) AS successful,
                    COALESCE(SUM(revenue_msats), 0) AS revenue_msats,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM analytics_model_minute
                WHERE minute_ts >= ?
                  AND model IN ({placeholders})
                GROUP BY bucket_ts, model
                ORDER BY bucket_ts
                """,
                (bucket_seconds, bucket_seconds, cutoff_timestamp, *top_models),
            ).fetchall()

            for row in top_model_rows:
                bucket_ts = str(row["bucket_ts"])
                bucket = bucket_index.get(bucket_ts)
                if bucket is None:
                    continue

                model = str(row["model"])
                successful = int(row["successful"])
                revenue_msats = float(row["revenue_msats"])
                total_tokens = int(row["total_tokens"])

                model_counts = bucket["model_counts"]
                model_counts[model] = successful
                model_revenue_msats = bucket["model_revenue_msats"]
                model_revenue_msats[model] = revenue_msats
                model_tokens = bucket["model_tokens"]
                model_tokens[model] = total_tokens

                bucket["others"] = max(0, int(bucket["others"]) - successful)
                bucket["others_revenue_msats"] = max(
                    0.0,
                    float(bucket["others_revenue_msats"]) - revenue_msats,
                )
                bucket["others_tokens"] = max(
                    0,
                    int(bucket["others_tokens"]) - total_tokens,
                )

        metrics = sorted(bucket_index.values(), key=lambda item: str(item["timestamp"]))

        return {
            "top_models": top_models,
            "metrics": metrics,
            "hours_back": hours_back,
            "interval_minutes": interval_minutes,
            "total_buckets": len(metrics),
        }

    def _cutoff_timestamp(self, hours_back: int) -> str:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        return cutoff.strftime("%Y-%m-%d %H:%M:%S")

    def _minute_key(self, timestamp: Any) -> str | None:
        if not isinstance(timestamp, str) or len(timestamp) != 19:
            return None
        if timestamp[10] != " ":
            return None
        return f"{timestamp[:16]}:00"

    def _extract_success_metrics(
        self, entry: dict[str, Any], message: str
    ) -> tuple[bool, float, int, int]:
        # These auth logs are emitted once per successful settlement across providers
        # and avoid duplicate counting from provider-specific completion logs.
        logger_name = str(entry.get("name", ""))
        if not logger_name.startswith("routstr.auth"):
            return False, 0.0, 0, 0

        input_tokens = self._parse_token_count(entry.get("input_tokens", 0))
        output_tokens = self._parse_token_count(entry.get("output_tokens", 0))

        if "calculated token-based cost" in message:
            token_cost = entry.get("token_cost", 0)
            if isinstance(token_cost, (int, float)) and token_cost > 0:
                return True, float(token_cost), input_tokens, output_tokens
            return True, 0.0, input_tokens, output_tokens

        if "max cost payment finalized" in message:
            charged_amount = entry.get("charged_amount", 0)
            if isinstance(charged_amount, (int, float)) and charged_amount > 0:
                return True, float(charged_amount), input_tokens, output_tokens
            return True, 0.0, input_tokens, output_tokens

        return False, 0.0, 0, 0

    def _parse_token_count(self, value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, float):
            return max(0, int(value))
        if isinstance(value, str):
            try:
                return max(0, int(float(value)))
            except ValueError:
                return 0
        return 0

    def _new_minute_stats(self) -> dict[str, float]:
        return {
            "total_entries": 0.0,
            "total_requests": 0.0,
            "successful_chat_completions": 0.0,
            "failed_requests": 0.0,
            "errors": 0.0,
            "warnings": 0.0,
            "payment_processed": 0.0,
            "upstream_errors": 0.0,
            "revenue_msats": 0.0,
            "refunds_msats": 0.0,
            "input_tokens": 0.0,
            "output_tokens": 0.0,
            "total_tokens": 0.0,
        }

    def _new_model_stats(self) -> dict[str, float]:
        return {
            "requests": 0.0,
            "successful": 0.0,
            "failed": 0.0,
            "revenue_msats": 0.0,
            "refunds_msats": 0.0,
            "input_tokens": 0.0,
            "output_tokens": 0.0,
            "total_tokens": 0.0,
        }
