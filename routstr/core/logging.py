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
   - Used as the successful completion fallback when token usage is unavailable
   - The 'charged_amount', 'model', 'input_tokens', and 'output_tokens' fields are extracted for dashboard metrics

4. "Payment processed successfully" (INFO) - routstr/auth.py
   - Used to count successful payment processing events
   - Tracks payment-related metrics

5. "Upstream request failed, revert payment" (WARNING) - routstr/proxy.py
   - Used to track failed requests and refunds
   - The 'max_cost_for_model' field is extracted for refund calculation
   - Must include 'max_cost_for_model' in extra dict

6. Any ERROR level logs with "upstream" in the message
   - Used to count upstream provider errors
   - Helps identify service reliability issues

If you need to modify these messages, ensure you also update the parsing logic in:
- routstr/core/usage_analytics_store.py
- routstr/core/log_manager.py
"""

import logging.config
import logging.handlers
import os
import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from pythonjsonlogger import jsonlogger
from rich.console import Console
from rich.logging import RichHandler

from .redaction import redact_obj, redact_org_ids

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

        # FIX ME: not sure if we need this
        # self._cleanup_old_files()

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

    def _scrub(self, message: str) -> str:
        """Redact secrets from a single string."""
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
        return message

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter out sensitive information from log records."""
        try:
            message = redact_org_ids(record.getMessage())
            record.msg = self._scrub(message)
            record.args = ()

            # The exception traceback is appended by the *formatter* (e.g. when a
            # QueueHandler flattens the record before it is announced to the
            # public, permanent SentryStr relays), so the message scrub above
            # never sees it. Pre-render and redact it here and cache the result
            # in `exc_text` so every formatter reuses the scrubbed version.
            if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = self._scrub(redact_org_ids(record.exc_text))
            if record.stack_info:
                record.stack_info = self._scrub(redact_org_ids(record.stack_info))

            # Structured `extra={...}` fields are emitted by the JSON formatter
            # straight from the record dict and never pass through the message
            # formatting above. Redact organization IDs from any string-valued
            # extra so they cannot leak via structured logs.
            for attr, value in list(record.__dict__.items()):
                if attr in _NON_EXTRA_RECORD_ATTRS:
                    continue
                if isinstance(value, (str, dict, list, tuple)):
                    record.__dict__[attr] = redact_obj(value)

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
                "()": DailyRotatingFileHandler,
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

    _install_sentrystr_logging(LOGGING_CONFIG)


# Handle for the background SentryStr logging worker (None when disabled).
_sentrystr_handle: Any = None

# Bound on the in-memory queue feeding the background SentryStr worker. Records
# beyond this are dropped rather than blocking the caller or growing memory
# without limit when a relay is slow or unreachable.
_SENTRYSTR_QUEUE_SIZE = 10_000

# Max seconds to wait for the queued backlog to flush on shutdown before
# abandoning it, so shutdown cannot hang on a slow or unreachable relay.
_SENTRYSTR_SHUTDOWN_TIMEOUT = 10.0


def _install_sentrystr_logging(logging_config: dict[str, Any]) -> None:
    """Mirror routstr application logs to Nostr relays via SentryStr.

    Announcing to relays blocks, so SentryStr's installer runs the publish on a
    dedicated background worker thread fed by an in-memory queue; the request /
    event-loop threads only pay the cost of enqueueing a record. Disabled unless
    `ENABLE_SENTRYSTR` is set. Any failure here is swallowed so optional logging
    can never take down startup.
    """
    global _sentrystr_handle

    if _sentrystr_handle is not None:
        return

    try:
        from .settings import settings
    except Exception:
        return

    if not getattr(settings, "enable_sentrystr", False):
        return

    routstr_logger = logging.getLogger("routstr")

    relays = list(settings.sentrystr_relays or settings.relays or [])
    if not relays:
        routstr_logger.warning(
            "SentryStr logging is enabled but no relays are configured; set "
            "SENTRYSTR_RELAYS or RELAYS. Skipping SentryStr logging."
        )
        return

    try:
        from sentrystr import install_sentrystr_logging
    except Exception:
        routstr_logger.warning(
            "SentryStr logging is enabled but the `sentrystr` package is not "
            "installed. Install it with `pip install sentrystr`. Skipping "
            "SentryStr logging."
        )
        return

    # Default to ERROR: these events are announced to public, permanent relays,
    # so only genuine problems should be mirrored unless the operator opts into
    # something more verbose via SENTRYSTR_LOG_LEVEL. (Keep this at or above
    # LOG_LEVEL; a lower value is gated out by the routstr loggers' own level.)
    level = (settings.sentrystr_log_level or "ERROR").upper()

    # routstr application loggers set propagate=False, so the handler must be
    # attached to each of them rather than relying on propagation to the root.
    routstr_loggers = [
        name
        for name in logging_config.get("loggers", {})
        if name == "routstr" or name.startswith("routstr.")
    ] or ["routstr"]

    try:
        _sentrystr_handle = install_sentrystr_logging(
            relays=relays,
            private_key=(settings.sentrystr_nsec or settings.nsec or None),
            level=level,
            loggers=routstr_loggers,
            recipient_pubkey=(settings.sentrystr_recipient_npub or None),
            platform="routstr",
            formatter=logging.Formatter("%(message)s"),
            # Run the same scrubbing/enrichment filters used by the other
            # handlers, on the calling thread, before records are handed to the
            # background worker. RequestIdFilter must run here because it reads a
            # ContextVar that is only set on the request thread; SecurityFilter
            # must run here so secrets are redacted before anything is announced
            # to the (public, permanent) relays. Passing them to the installer
            # attaches them before the handler goes live, so no record can slip
            # through unfiltered during startup.
            filters=[RequestIdFilter(), SecurityFilter()],
            # Bound the queue so a slow/unreachable relay can't grow memory.
            queue_size=_SENTRYSTR_QUEUE_SIZE,
        )
    except Exception as exc:
        routstr_logger.warning("Failed to initialize SentryStr logging: %s", exc)
        return

    routstr_logger.info(
        "SentryStr logging enabled",
        extra={"sentrystr_level": level, "sentrystr_relay_count": len(relays)},
    )


def shutdown_sentrystr_logging() -> None:
    """Flush and stop the background SentryStr logging worker, if running."""
    global _sentrystr_handle
    handle = _sentrystr_handle
    _sentrystr_handle = None
    if handle is not None:
        try:
            handle.close(timeout=_SENTRYSTR_SHUTDOWN_TIMEOUT)
        except Exception:
            pass


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for the given module name."""
    return logging.getLogger(name)
