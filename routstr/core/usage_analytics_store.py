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

    SCHEMA_VERSION = "2"

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

            return {
                "metrics": metrics,
                "summary": summary,
                "error_details": error_details,
                "revenue_by_model": revenue_by_model,
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
        if current_version != self.SCHEMA_VERSION:
            self._drop_index_tables_locked(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO analytics_meta (key, value)
                VALUES ('schema_version', ?)
                """,
                (self.SCHEMA_VERSION,),
            )

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
                refunds_msats REAL NOT NULL DEFAULT 0
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
            "CREATE INDEX IF NOT EXISTS idx_analytics_model_presence_ts ON analytics_model_presence_minute (minute_ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_error_type_minute_ts ON analytics_error_type_minute (minute_ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_error_events_ts ON analytics_error_events (timestamp DESC)"
        )
        conn.commit()

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

                completed = (
                    "completed for streaming" in message
                    or "completed for non-streaming" in message
                )
                if completed:
                    bucket["total_requests"] += 1
                    bucket["successful_chat_completions"] += 1
                    model_bucket = model_updates[(minute_key, model)]
                    model_bucket["requests"] += 1
                    model_bucket["successful"] += 1

                    cost_data = entry.get("cost_data")
                    if isinstance(cost_data, dict):
                        actual_cost = cost_data.get("total_msats", 0)
                        if isinstance(actual_cost, (int, float)) and actual_cost > 0:
                            cost_float = float(actual_cost)
                            bucket["revenue_msats"] += cost_float
                            model_bucket["revenue_msats"] += cost_float

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
                    refunds_msats
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    refunds_msats = refunds_msats + excluded.refunds_msats
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
                    refunds_msats
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(minute_ts, model) DO UPDATE SET
                    requests = requests + excluded.requests,
                    successful = successful + excluded.successful,
                    failed = failed + excluded.failed,
                    revenue_msats = revenue_msats + excluded.revenue_msats,
                    refunds_msats = refunds_msats + excluded.refunds_msats
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
                COALESCE(SUM(refunds_msats), 0) AS refunds_msats
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

            totals["total_requests"] += total_requests
            totals["successful_chat_completions"] += successful
            totals["failed_requests"] += failed
            totals["errors"] += errors
            totals["warnings"] += warnings
            totals["payment_processed"] += payment_processed
            totals["upstream_errors"] += upstream_errors
            totals["revenue_msats"] += revenue_msats
            totals["refunds_msats"] += refunds_msats

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
                COALESCE(SUM(refunds_msats), 0) AS refunds_msats
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

    def _cutoff_timestamp(self, hours_back: int) -> str:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        return cutoff.strftime("%Y-%m-%d %H:%M:%S")

    def _minute_key(self, timestamp: Any) -> str | None:
        if not isinstance(timestamp, str) or len(timestamp) != 19:
            return None
        if timestamp[10] != " ":
            return None
        return f"{timestamp[:16]}:00"

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
        }

    def _new_model_stats(self) -> dict[str, float]:
        return {
            "requests": 0.0,
            "successful": 0.0,
            "failed": 0.0,
            "revenue_msats": 0.0,
            "refunds_msats": 0.0,
        }
