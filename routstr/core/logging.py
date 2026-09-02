"""
Logging configuration for Routstr.

CRITICAL LOG MESSAGES FOR USAGE STATISTICS:
===========================================
The following log messages are parsed by the usage tracking system
(routstr/core/usage_analytics_store.py and routstr/core/log_manager.py).
DO NOT modify or remove these messages without updating the usage tracking logic:

1. "Received proxy request" (INFO) - routstr/proxy.py
   - Used to count total incoming requests
   - Includes model information in context

2. "Calculated token-based cost" (INFO) - routstr/auth.py
   - Used to track successful completions and revenue
   - The 'token_cost', 'model', 'input_tokens', and 'output_tokens' fields are extracted for dashboard metrics

3. "Max cost payment finalized" (INFO) - routstr/auth.py
   - Used for explicit flat-price/MaxCostData settlements; missing usage alone must not create this charge
   - The 'charged_amount', 'model', 'input_tokens', and 'output_tokens' fields are extracted for dashboard metrics

4. "Payment processed successfully" (INFO) - routstr/auth.py
   - Used to count successful payment processing events
   - Tracks payment-related metrics

5. "Upstream request failed, revert payment" (WARNING) - routstr/proxy.py
   - Used to track failed requests and refunds
   - The 'max_cost_for_model' field is extracted for refund calculation
   - Must include 'max_cost_for_model' in extra dict

6. "Payment settlement finished" (INFO) - routstr/auth.py and routstr/upstream/ehbp.py
   - Emitted once per settlement attempt, including EHBP settlements
   - Carries 'settlement_duration_ms' and 'settlement_succeeded'; the EHBP
     emitter adds 'settlement_type'

7. Any ERROR level logs with "upstream" in the message
   - Used to count upstream provider errors
   - Helps identify service reliability issues

If you need to modify these messages, ensure you also update the parsing logic in:
- routstr/core/usage_analytics_store.py
- routstr/core/log_manager.py
"""

import copy
import logging.config
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from pythonjsonlogger import jsonlogger
from rich.console import Console
from rich.logging import RichHandler

from .redaction import redact_field, redact_org_ids

# Only use RichHandler when stdout is a real TTY. In non-TTY contexts
# (docker logs, pipes, CI) Rich pads every line to width and wraps long
# records, producing visually-empty trailing whitespace and split records.
# A plain StreamHandler avoids both problems.
_stdout_is_tty = sys.stdout.isatty()
_console = Console(soft_wrap=True) if _stdout_is_tty else None

# Define custom TRACE level
TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")


def trace(self: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    """Log with TRACE level"""
    if self.isEnabledFor(TRACE_LEVEL):
        self._log(TRACE_LEVEL, message, args, **kwargs)


# Add the trace method to Logger class
setattr(logging.Logger, "trace", trace)


class DailyRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Custom TimedRotatingFileHandler that creates date-based filenames."""

    def __init__(self, filename: str, **kwargs: Any) -> None:
        """Initialize with a base filename pattern."""
        self.base_dir = os.path.dirname(filename)
        self.base_name = os.path.basename(filename).replace(".log", "")

        today = datetime.now().strftime("%Y-%m-%d")
        self.current_date = today
        dated_filename = os.path.join(self.base_dir, f"{self.base_name}_{today}.log")

        super().__init__(dated_filename, **kwargs)

    def doRollover(self) -> None:
        """Override rollover to create new date-based filename."""
        if self.stream:
            self.stream.close()

        new_date = datetime.now().strftime("%Y-%m-%d")
        new_filename = os.path.join(self.base_dir, f"{self.base_name}_{new_date}.log")

        self.baseFilename = new_filename
        self.current_date = new_date

        # `backupCount` alone never prunes these files: the base filename moves
        # with the date, so the inherited rollover finds no siblings to expire
        # and every day of logged credentials is retained indefinitely.
        self._cleanup_old_files()

        if not self.delay:
            self.stream = self._open()

    def _cleanup_old_files(self) -> None:
        """Remove old log files beyond backupCount."""
        if self.backupCount > 0:
            log_files = []
            if os.path.exists(self.base_dir):
                for file in os.listdir(self.base_dir):
                    if file.startswith(f"{self.base_name}_") and file.endswith(".log"):
                        file_path = os.path.join(self.base_dir, file)
                        log_files.append((file_path, os.path.getmtime(file_path)))

            log_files.sort(key=lambda x: x[1], reverse=True)

            for file_path, _ in log_files[self.backupCount :]:
                try:
                    os.remove(file_path)
                except OSError:
                    pass


class QueuedDailyRotatingFileHandler(logging.Handler):
    """Move rotating-file I/O off request threads.

    When both locks are needed, acquire the logging module lock before the
    handler lock to match ``dictConfig``.
    """

    _queue: queue.Queue[logging.LogRecord]
    _target: DailyRotatingFileHandler
    _listener: logging.handlers.QueueListener
    _drain_timeout_seconds = 5.0
    _reopen_backoff_seconds = 5.0

    def __init__(self, filename: str, **kwargs: Any) -> None:
        super().__init__()
        self._filename = filename
        self._kwargs = kwargs
        self._stopped = True
        self._next_open_attempt = 0.0
        self._open()

    def _open(self) -> None:
        """Attach a fresh rotating file handler and start draining it."""
        # A new queue per listener: QueueListener's stop sentinel is a shared
        # singleton, so two listeners on one queue would steal each other's.
        record_queue: queue.Queue[logging.LogRecord] = queue.Queue()
        target = DailyRotatingFileHandler(self._filename, **self._kwargs)
        target.setFormatter(self.formatter)
        listener = logging.handlers.QueueListener(record_queue, target)
        try:
            listener.start()
        except Exception:
            target.close()
            raise

        self._queue = record_queue
        self._target = target
        self._listener = listener
        self._stopped = False
        self._closed = False
        with getattr(logging, "_lock"):
            handler_list = getattr(logging, "_handlerList")
            # This wrapper owns the target's shutdown and lock ordering.
            handler_list[:] = [
                reference for reference in handler_list if reference() is not target
            ]
            if not any(reference() is self for reference in handler_list):
                getattr(logging, "_addHandlerRef")(self)

    def _reopen_locked(self) -> bool:
        """Reopen using the lock order required by ``dictConfig``."""
        with getattr(logging, "_lock"):
            self.acquire()
            try:
                if not self._stopped:
                    return True
                if time.monotonic() < self._next_open_attempt:
                    return False
                try:
                    self._open()
                except Exception:
                    self._next_open_attempt = (
                        time.monotonic() + self._reopen_backoff_seconds
                    )
                    raise
                return True
            finally:
                self.release()

    def setFormatter(self, fmt: logging.Formatter | None) -> None:
        super().setFormatter(fmt)
        self._target.setFormatter(fmt)

    def handle(self, record: logging.LogRecord) -> bool:
        if not self.filter(record):
            return False

        while True:
            self.acquire()
            try:
                if not self._stopped:
                    self.emit(record)
                    return True
            finally:
                self.release()

            try:
                # Do not acquire the module lock while holding the handler lock.
                if not self._reopen_locked():
                    return False
            except Exception:
                self.handleError(record)
                return False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self._stopped:
                self._queue.put_nowait(copy.copy(record))
        except Exception:
            # Handler.handle() does not catch exceptions raised by emit().
            self.handleError(record)

    def flush(self) -> None:
        self.acquire()
        try:
            if self._stopped:
                return

            deadline = time.monotonic() + self._drain_timeout_seconds
            with self._queue.all_tasks_done:
                while self._queue.unfinished_tasks:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._queue.all_tasks_done.wait(remaining)
            self._target.flush()
        finally:
            self.release()

    def _stop_listener(self) -> bool:
        thread = self._listener._thread
        if thread is None:
            return True
        self._listener.enqueue_sentinel()
        thread.join(timeout=self._drain_timeout_seconds)
        if thread.is_alive():
            return False
        self._listener._thread = None
        return True

    def _close_retired_listener(
        self,
        listener: logging.handlers.QueueListener,
        target: DailyRotatingFileHandler,
    ) -> None:
        def finish() -> None:
            thread = listener._thread
            if thread is not None:
                thread.join()
                listener._thread = None
            try:
                target.flush()
            finally:
                target.close()

        threading.Thread(target=finish, daemon=True).start()

    def close(self) -> None:
        # Stop the listener under the handler lock, then close the target outside
        # it because FileHandler.close() also takes the logging module lock.
        self.acquire()
        try:
            target = None
            retired = None
            if not self._stopped:
                listener = self._listener
                current_target = self._target
                stopped = self._stop_listener()
                self._stopped = True
                if stopped:
                    target = current_target
                else:
                    retired = (listener, current_target)
                    sys.stderr.write(
                        f"Logging listener for {self._filename} did not stop "
                        f"within {self._drain_timeout_seconds}s; reopening on next record\n"
                    )
        finally:
            self.release()

        if retired is not None:
            self._close_retired_listener(*retired)
        if target is not None:
            target.flush()
            target.close()
        super().close()


def get_package_version() -> str:
    """Read the package version from pyproject.toml."""
    try:
        # Find project root by looking for pyproject.toml
        current_path = Path(__file__).parent
        while current_path != current_path.parent:
            pyproject_path = current_path / "pyproject.toml"
            if pyproject_path.exists():
                with open(pyproject_path, "rb") as f:
                    pyproject_data = tomllib.load(f)
                version = pyproject_data.get("project", {}).get("version", "unknown")
                return version
            current_path = current_path.parent

        # Fallback: try the simple path resolution (3 levels up for routstr/logging/logging_config.py)
        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        if pyproject_path.exists():
            with open(pyproject_path, "rb") as f:
                pyproject_data = tomllib.load(f)
            version = pyproject_data.get("project", {}).get("version", "unknown")
            return version

        return "unknown"
    except Exception:
        return "unknown"


class VersionFilter(logging.Filter):
    """Filter to add package version to all log records."""

    def __init__(self) -> None:
        super().__init__()
        self.version = get_package_version()

    def filter(self, record: logging.LogRecord) -> bool:
        """Add version information to the log record."""
        record.version = self.version
        return True


class RequestIdFilter(logging.Filter):
    """Filter to add request ID to all log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add request ID to the log record if available."""
        try:
            # Import here to avoid circular imports
            from .middleware import request_id_context

            request_id = request_id_context.get(None)
            record.request_id = request_id if request_id else "no-request-id"
        except ImportError:
            # If middleware isn't available yet, just use default
            record.request_id = "no-request-id"
        return True


# Standard ``LogRecord`` attributes that are never user-supplied ``extra``
# fields; skipped when redacting structured extras (``msg``/``message`` are
# handled separately above).
_NON_EXTRA_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
    }
)


class SecurityFilter(logging.Filter):
    """Filter to remove sensitive information from logs."""

    SENSITIVE_KEYS = {
        "authorization",
        "x-cashu",
        "bearer",
        "token",
        "key",
        "secret",
        "password",
        "cashu_token",
        "bearer_key",
        "api_key",
        "nsec",
        "upstream_api_key",
        "refund_address",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter out sensitive information from log records."""
        try:
            message = record.getMessage()
            message = redact_org_ids(message)
            standalone_patterns = [
                r"Bearer\s+([a-zA-Z0-9_\-\.]{10,})",  # Bearer token (must be 10 characters or more to reduce false-positives)
                r"cashu[A-Z]+([a-zA-Z0-9_\-\.=/+]+)",  # Cashu tokens
                r"nsec[a-z0-9]+",  # Nostr Public / Private Key
            ]
            for pattern in standalone_patterns:
                message = re.sub(pattern, "[REDACTED]", message, flags=re.IGNORECASE)

            for key in self.SENSITIVE_KEYS:
                if key in message.lower():
                    key_patterns = [
                        rf"{key}\s*[:=]\s*([a-zA-Z0-9_\-\.=/+]+)",  # key:value or key=value (including any variant with spaces)
                        rf'{key}\s*[:=]\s*["\']([^"\']+)["\']',  # key:"value" or key='value' (including any variant with spaces)
                    ]
                    for pattern in key_patterns:
                        message = re.sub(
                            pattern, f"{key}: [REDACTED]", message, flags=re.IGNORECASE
                        )
            record.msg = message
            record.args = ()

            # Structured `extra={...}` fields are emitted by the JSON formatter
            # straight from the record dict and never pass through the message
            # formatting above, so they need their own recursive pass.
            for attr, value in list(record.__dict__.items()):
                if attr in _NON_EXTRA_RECORD_ATTRS:
                    continue
                record.__dict__[attr] = redact_field(attr, value)

        except Exception:
            pass

        return True


def get_log_level() -> str:
    """Get log level from environment variable."""
    try:
        from .settings import settings

        level = settings.log_level.upper()
    except Exception:
        level = os.environ.get("LOG_LEVEL", "INFO").upper()
    # Validate log level - if invalid, default to INFO
    valid_levels = {"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if level not in valid_levels:
        level = "INFO"
    return level


def should_enable_console_logging() -> bool:
    """Check if console logging should be enabled."""
    try:
        from .settings import settings

        return bool(settings.enable_console_logging)
    except Exception:
        return os.environ.get("ENABLE_CONSOLE_LOGGING", "true").lower() in (
            "true",
            "1",
            "yes",
        )


def setup_logging() -> None:
    """Configure centralized logging for the application."""

    log_level = get_log_level()
    console_enabled = should_enable_console_logging()

    # Determine which handlers to use
    handlers = ["file"]
    if console_enabled:
        handlers.append("console")

    if _stdout_is_tty:
        console_handler: dict[str, Any] = {
            "()": RichHandler,
            "level": log_level,
            "show_time": False,
            "show_path": False,
            "rich_tracebacks": True,
            "markup": True,
            "console": _console,
            "filters": ["request_id_filter", "security_filter"],
        }
    else:
        console_handler = {
            "class": "logging.StreamHandler",
            "level": log_level,
            "formatter": "plain",
            "stream": "ext://sys.stdout",
            "filters": ["request_id_filter", "security_filter"],
        }

    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": jsonlogger.JsonFormatter,
                "format": "%(asctime)s %(name)s %(levelname)s %(message)s %(pathname)s %(lineno)d %(version)s %(request_id)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "plain": {
                "format": "%(asctime)s %(levelname)-7s %(name)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "filters": {
            "version_filter": {"()": VersionFilter},
            "request_id_filter": {"()": RequestIdFilter},
            "security_filter": {"()": SecurityFilter},
        },
        "handlers": {
            "console": console_handler,
            "file": {
                "()": QueuedDailyRotatingFileHandler,
                "level": log_level,
                "formatter": "json",
                "filename": "logs/app.log",
                "when": "midnight",  # Rotate at midnight each day
                "interval": 1,  # Every 1 day
                "backupCount": 30,  # Keep 30 days of logs
                "atTime": None,  # Rotate at midnight (00:00)
                "filters": ["version_filter", "request_id_filter", "security_filter"],
            },
        },
        "loggers": {
            "routstr": {
                "level": log_level,
                "handlers": handlers,
                "propagate": False,
            },
            "routstr.payment": {
                "level": log_level,
                "handlers": handlers,
                "propagate": False,
            },
            "routstr.proxy": {
                "level": log_level,
                "handlers": handlers,
                "propagate": False,
            },
            "routstr.auth": {
                "level": log_level,
                "handlers": handlers,
                "propagate": False,
            },
            "routstr.payment.models": {
                "level": log_level,
                "handlers": handlers,
                "propagate": False,
            },
            "routstr.core.exceptions": {
                "level": log_level,
                "handlers": handlers,
                "propagate": False,
            },
            "routstr.core.middleware": {
                "level": log_level,
                "handlers": ["file"],
                "propagate": False,
            },
            # Suppress verbose third-party logging
            "httpx": {
                "level": "WARNING",
                "handlers": ["console"] if console_enabled else [],
                "propagate": False,
            },
            "openai": {
                "level": "WARNING",
                "handlers": ["console"] if console_enabled else [],
                "propagate": False,
            },
            "httpcore": {
                "level": "WARNING",
                "handlers": ["console"] if console_enabled else [],
                "propagate": False,
            },
            "websockets": {
                "level": "WARNING",
                "handlers": [],
                "propagate": False,
            },
            "uvicorn.access": {
                "level": "WARNING",
                "handlers": ["file"],
                "propagate": False,
            },
            "uvicorn.error": {
                "level": log_level,
                "handlers": handlers,
                "propagate": False,
            },
            "watchfiles.main": {"level": "WARNING", "handlers": [], "propagate": False},
            "aiosqlite": {"level": "ERROR", "handlers": [], "propagate": False},
            "alembic": {
                "level": "WARNING",
                "handlers": ["console"] if console_enabled else [],
                "propagate": False,
            },
        },
        "root": {
            "level": log_level,
            "handlers": ["console"] if console_enabled else [],
        },
    }

    os.makedirs("logs", exist_ok=True)

    logging.config.dictConfig(LOGGING_CONFIG)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for the given module name."""
    return logging.getLogger(name)
