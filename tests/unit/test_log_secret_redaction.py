"""Regression tests for spendable credentials leaking into the dated log files.

Everything here asserts against the bytes the ``DailyRotatingFileHandler``
actually wrote to disk. Asserting against a mock would pass even if the JSON
formatter emitted the raw ``extra`` dict, which is exactly the bug.
"""

import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pythonjsonlogger import jsonlogger

from routstr.core.logging import (
    DailyRotatingFileHandler,
    RequestIdFilter,
    SecurityFilter,
    VersionFilter,
)
from routstr.core.middleware import LoggingMiddleware

REFUND_TOKEN = (
    "cashuBo2FteCJodHRwczovL21pbnQubWluaWJpdHMuY2FzaC9CaXRjb2luYXVjc"
    "2F0YXSBomFpSAA5tMOFA4EXYXCBo2FhAmFzeEA5NmY0NTFhZjMzMGY3ZmM2ZGY5"
)
HASHED_KEY = "b3d9f1c2a8574e60b7c1f0aa9d2e4c85f6b70a1932de84cc57bf90ae1d2c3f47"


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir()
    return directory


@pytest.fixture
def handler(log_dir: Path) -> Iterator[DailyRotatingFileHandler]:
    """A file handler configured exactly like the production ``file`` handler."""
    handler = DailyRotatingFileHandler(
        str(log_dir / "app.log"),
        when="midnight",
        interval=1,
        backupCount=30,
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s %(pathname)s "
            "%(lineno)d %(version)s %(request_id)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    for log_filter in (VersionFilter(), RequestIdFilter(), SecurityFilter()):
        handler.addFilter(log_filter)
    try:
        yield handler
    finally:
        handler.close()


@pytest.fixture
def emit(handler: DailyRotatingFileHandler) -> Callable[..., str]:
    """Log one record and return the raw text of the dated file it landed in."""
    logger = logging.getLogger("routstr.test.redaction")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers = [handler]

    def _emit(message: str, **extra: Any) -> str:
        logger.info(message, extra=extra)
        handler.flush()
        return Path(handler.baseFilename).read_text()

    return _emit


def test_refund_token_never_reaches_the_dated_file(emit: Callable[..., str]) -> None:
    written = emit(
        "refund_wallet_endpoint: cashu token issued",
        token=REFUND_TOKEN,
        amount=1500,
        currency="sat",
    )
    assert REFUND_TOKEN not in written
    assert "1500" in written


def test_authorization_values_never_reach_the_dated_file(
    emit: Callable[..., str],
) -> None:
    written = emit(
        "Incoming request",
        authorization=f"Bearer sk-{HASHED_KEY}",
        headers={"Authorization": f"Bearer sk-{HASHED_KEY}"},
    )
    assert HASHED_KEY not in written
    assert "Bearer sk-" not in written


def test_full_key_hashes_never_reach_the_dated_file(emit: Callable[..., str]) -> None:
    written = emit(
        "refund_wallet_endpoint: balance restored after mint failure",
        hashed_key=HASHED_KEY,
        key_hash=HASHED_KEY,
        restored_balance=42,
    )
    assert HASHED_KEY not in written
    assert "42" in written


def test_secrets_nested_in_dicts_and_lists_never_reach_the_dated_file(
    emit: Callable[..., str],
) -> None:
    written = emit(
        "Upstream call failed",
        context={
            "attempts": [
                {"headers": {"authorization": f"Bearer sk-{HASHED_KEY}"}},
                {"body": {"refund": {"token": REFUND_TOKEN}}},
            ],
            "provider": "openai",
        },
    )
    assert HASHED_KEY not in written
    assert REFUND_TOKEN not in written
    assert "openai" in written


def test_query_string_secrets_never_reach_the_dated_file(
    emit: Callable[..., str],
) -> None:
    written = emit(
        "Incoming request",
        path="/v1/wallet/refund",
        query_params={"api_key": f"sk-{HASHED_KEY}", "page": "2"},
        target=f"/v1/wallet/refund?token={REFUND_TOKEN}",
    )
    assert HASHED_KEY not in written
    assert REFUND_TOKEN not in written
    assert "/v1/wallet/refund" in written


def test_benign_telemetry_is_not_redacted(emit: Callable[..., str]) -> None:
    written = emit(
        "Request completed",
        method="POST",
        path="/v1/chat/completions",
        model="gpt-4o-mini",
        status_code=200,
        duration_ms=13.5,
        input_tokens=120,
        key_hash=HASHED_KEY[:8],
        mint_url="https://mint.minibits.cash/Bitcoin",
    )
    record = json.loads(written.strip().splitlines()[-1])
    assert record["model"] == "gpt-4o-mini"
    assert record["status_code"] == 200
    assert record["duration_ms"] == 13.5
    assert record["input_tokens"] == 120
    assert record["key_hash"] == HASHED_KEY[:8]
    assert record["mint_url"] == "https://mint.minibits.cash/Bitcoin"
    assert record["message"] == "Request completed"


def test_self_referential_extra_does_not_hang_the_logger(
    emit: Callable[..., str],
) -> None:
    cyclic: dict[str, Any] = {"token": REFUND_TOKEN}
    cyclic["self"] = cyclic
    deep: dict[str, Any] = {"token": REFUND_TOKEN}
    for _ in range(200):
        deep = {"nested": deep}

    written = emit("Cyclic payload", context=cyclic, deep=deep)

    assert REFUND_TOKEN not in written
    assert json.loads(written.strip().splitlines()[-1])["message"] == "Cyclic payload"


def test_middleware_logs_query_param_names_without_values(
    handler: DailyRotatingFileHandler,
) -> None:
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/v1/wallet/refund")
    async def refund() -> dict[str, str]:
        return {"status": "ok"}

    middleware_logger = logging.getLogger("routstr.core.middleware")
    middleware_logger.setLevel(logging.INFO)
    middleware_logger.propagate = False
    original_handlers = middleware_logger.handlers
    middleware_logger.handlers = [handler]
    try:
        with TestClient(app) as client:
            response = client.get(
                "/v1/wallet/refund", params={"api_key": f"sk-{HASHED_KEY}", "page": "2"}
            )
        assert response.status_code == 200
    finally:
        middleware_logger.handlers = original_handlers

    handler.flush()
    written = Path(handler.baseFilename).read_text()
    assert HASHED_KEY not in written
    record = json.loads(written.strip().splitlines()[0])
    assert record["path"] == "/v1/wallet/refund"
    assert record["query_param_names"] == ["api_key", "page"]


def test_forced_rollover_enforces_the_retention_limit(
    handler: DailyRotatingFileHandler, log_dir: Path
) -> None:
    handler.backupCount = 3
    handler.emit(
        logging.LogRecord("t", logging.INFO, "", 0, "current", (), None),
    )
    handler.flush()

    now = time.time()
    for day in range(1, 6):
        stale = log_dir / f"app_2024-01-0{day}.log"
        stale.write_text("stale\n")
        os.utime(stale, (now - day * 86400, now - day * 86400))

    handler.doRollover()

    remaining = sorted(p.name for p in log_dir.glob("app_*.log"))
    assert len(remaining) == handler.backupCount
    assert Path(handler.baseFilename).name in remaining
