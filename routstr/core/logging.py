"""
Logging configuration for Routstr.

CRITICAL LOG MESSAGES FOR USAGE STATISTICS:
===========================================
The following log messages are parsed by the usage tracking system (routstr/core/admin.py).
DO NOT modify or remove these messages without updating the usage tracking logic:

1. "Received proxy request" (INFO) - routstr/proxy.py
   - Used to count total incoming requests
   - Includes model information in context

    2. "Payment adjustment completed for streaming" (INFO) - routstr/upstream/base.py
       "Payment adjustment completed for non-streaming" (INFO) - routstr/upstream/base.py
   - Used to track successful completions and revenue
   - The 'cost_data.total_msats' field is extracted for revenue calculation
   - Must include 'cost_data' in extra dict

3. "Payment processed successfully" (INFO) - routstr/auth.py
   - Used to count successful payment processing events
   - Tracks payment-related metrics

4. "Upstream request failed, revert payment" (WARNING) - routstr/proxy.py
   - Used to track failed requests and refunds
   - The 'max_cost_for_model' field is extracted for refund calculation
   - Must include 'max_cost_for_model' in extra dict

5. Any ERROR level logs with "upstream" in the message
   - Used to count upstream provider errors
   - Helps identify service reliability issues

If you need to modify these messages, ensure you also update the parsing logic in:
- routstr/core/admin.py:_aggregate_metrics_by_time()
- routstr/core/admin.py:_get_summary_stats()
- routstr/core/admin.py:get_revenue_by_model()
"""

import logging.config
import logging.handlers
import os
import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from pythonjsonlogger import jsonlogger
from rich.logging import RichHandler

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

            for key in self.SENSITIVE_KEYS:
                if key in message.lower():
                    patterns = [
                        rf"{key}[:\s=]+([a-zA-Z0-9_\-\.]+)",  # key: value or key=value
                        rf'{key}[:\s=]+["\']([^"\']+)["\']',  # key: "value" or key='value'
                        r"Bearer\s+([a-zA-Z0-9_\-\.]+)",  # Bearer token
                        r"cashu[A-Z]+([a-zA-Z0-9_\-\.=/+]+)",  # Cashu tokens
                    ]

                    for pattern in patterns:
                        message = re.sub(
                            pattern, f"{key}: [REDACTED]", message, flags=re.IGNORECASE
                        )

            record.msg = message
            record.args = ()

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

    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": jsonlogger.JsonFormatter,
                "format": "%(asctime)s %(name)s %(levelname)s %(message)s %(pathname)s %(lineno)d %(version)s %(request_id)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "filters": {
            "version_filter": {"()": VersionFilter},
            "request_id_filter": {"()": RequestIdFilter},
            "security_filter": {"()": SecurityFilter},
        },
        "handlers": {
            "console": {
                "()": RichHandler,
                "level": log_level,
                "show_time": False,
                "show_path": False,
                "rich_tracebacks": True,
                "markup": True,
                "filters": ["request_id_filter", "security_filter"],
            },
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
