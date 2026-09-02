import logging
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import routstr.core.logging as routstr_logging
from routstr.core.logging import QueuedDailyRotatingFileHandler


def _log_text(tmp_path: Path) -> str:
    return "".join(path.read_text() for path in sorted(tmp_path.glob("app_*.log")))


def _make_handler(
    tmp_path: Path, name: str
) -> tuple[logging.Logger, QueuedDailyRotatingFileHandler]:
    handler = QueuedDailyRotatingFileHandler(
        str(tmp_path / "app.log"), when="midnight", backupCount=1
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.Logger(name)
    logger.addHandler(handler)
    return logger, handler


def test_queued_file_handler_flushes_records_on_close(tmp_path: Path) -> None:
    logger, handler = _make_handler(tmp_path, "queued-file-test")
    try:
        logger.info("written from listener")
        handler.flush()

        assert "written from listener" in _log_text(tmp_path)
    finally:
        handler.close()


def test_queued_file_handler_loses_no_records_on_close(tmp_path: Path) -> None:
    logger, handler = _make_handler(tmp_path, "queued-file-drain-test")
    try:
        for index in range(400):
            logger.info("Payment processed successfully %d", index)
    finally:
        handler.close()

    written = _log_text(tmp_path)
    assert written.count("Payment processed successfully") == 400


def test_queued_file_handler_keeps_logging_after_close(tmp_path: Path) -> None:
    """dictConfig closes live handlers; uvicorn runs one after app import."""
    logger, handler = _make_handler(tmp_path, "queued-file-reopen-test")
    logger.info("before close")
    handler.close()

    logger.info("after close")
    handler.close()
    assert "after close" in _log_text(tmp_path)

    handler_list = getattr(logging, "_handlerList")
    handler_list[:] = [
        reference for reference in handler_list if reference() is not handler
    ]
    handler.close()
    logger.info("after reopen")
    assert any(reference() is handler for reference in handler_list)
    logging.shutdown(
        handlerList=[reference for reference in handler_list if reference() is handler]
    )
    assert "after reopen" in _log_text(tmp_path)


def test_queued_file_handler_contains_reopen_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, handler = _make_handler(tmp_path, "queued-file-failure-test")
    handler.close()

    attempts = 0

    def fail_to_open(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("disk unavailable")

    errors: list[logging.LogRecord] = []
    monkeypatch.setattr(routstr_logging, "DailyRotatingFileHandler", fail_to_open)
    monkeypatch.setattr(type(handler), "handleError", lambda _self, r: errors.append(r))

    for _ in range(50):
        logger.info("must not reach billing")

    assert attempts == 1
    assert len(errors) == 1
    handler.close()


def test_queued_file_handler_emit_does_not_raise_into_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, handler = _make_handler(tmp_path, "queued-file-emit-failure-test")
    handled: list[logging.LogRecord] = []

    class BrokenQueue:
        def put_nowait(self, _record: logging.LogRecord) -> None:
            raise OSError("queue is gone")

    monkeypatch.setattr(handler, "_queue", BrokenQueue())
    monkeypatch.setattr(type(handler), "handleError", lambda _s, r: handled.append(r))

    logger.info("settlement line")

    assert len(handled) == 1
    handler.close()


def test_queued_file_handler_recovers_after_close_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, handler = _make_handler(tmp_path, "queued-file-timeout-test")
    listener_blocked = threading.Event()
    allow_listener = threading.Event()
    old_target = handler._target
    original_handle = old_target.handle
    original_close = old_target.close
    target_closed = threading.Event()
    close_count = 0

    def blocked_handle(record: logging.LogRecord) -> bool:
        listener_blocked.set()
        assert allow_listener.wait(timeout=10)
        return original_handle(record)

    def track_close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()
        target_closed.set()

    monkeypatch.setattr(old_target, "handle", blocked_handle)
    monkeypatch.setattr(old_target, "close", track_close)
    handler._drain_timeout_seconds = 0.01
    logger.info("blocked record")
    assert listener_blocked.wait(timeout=10)

    handler.close()
    logger.info("record after timeout")
    allow_listener.set()
    handler.close()

    assert target_closed.wait(timeout=10)
    assert close_count == 1
    assert "record after timeout" in _log_text(tmp_path)


def test_queued_file_handler_reopens_when_close_wins_emit_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger, handler = _make_handler(tmp_path, "queued-file-atomic-race-test")
    emitter_waiting = threading.Event()
    allow_emitter = threading.Event()
    original_acquire = handler.acquire
    emitter_thread: threading.Thread | None = None
    gated = True

    def gated_acquire() -> None:
        nonlocal gated
        if gated and threading.current_thread() is emitter_thread:
            gated = False
            emitter_waiting.set()
            assert allow_emitter.wait(timeout=10)
        original_acquire()

    monkeypatch.setattr(handler, "acquire", gated_acquire)
    emitter_thread = threading.Thread(target=logger.info, args=("racing record",))
    try:
        emitter_thread.start()
        assert emitter_waiting.wait(timeout=10)

        handler.close()
        allow_emitter.set()
        emitter_thread.join(timeout=10)
        assert not emitter_thread.is_alive()

        handler.close()
        assert "racing record" in _log_text(tmp_path)
    finally:
        allow_emitter.set()
        handler.close()


def test_queued_file_handler_survives_close_racing_with_emit(tmp_path: Path) -> None:
    logger, handler = _make_handler(tmp_path, "queued-file-race-test")
    done = threading.Event()

    def spam() -> None:
        while not done.is_set():
            logger.info("racing record")

    def churn() -> None:
        for _ in range(50):
            handler.close()

    emitter = threading.Thread(target=spam, daemon=True)
    closer = threading.Thread(target=churn, daemon=True)
    try:
        emitter.start()
        closer.start()

        closer.join(timeout=10)
        done.set()
        emitter.join(timeout=10)

        assert not closer.is_alive(), "close() deadlocked against a concurrent emit()"
        assert not emitter.is_alive(), "emit() deadlocked against a concurrent close()"

        logger.info("final record")
        handler.flush()
        assert "final record" in _log_text(tmp_path)
    finally:
        done.set()
        handler.close()


def test_queued_file_handler_does_not_deadlock_against_dictconfig(
    tmp_path: Path,
) -> None:
    script = textwrap.dedent(
        """
        import logging
        import logging.config
        import sys
        import threading
        import time
        from pathlib import Path

        from routstr.core.logging import QueuedDailyRotatingFileHandler

        log_dir = Path(sys.argv[1])
        handler = QueuedDailyRotatingFileHandler(
            str(log_dir / "app.log"), when="midnight", backupCount=1
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.Logger("queued-file-dictconfig-test")
        logger.addHandler(handler)
        emitted = threading.Event()

        def spam():
            for _ in range(100):
                logger.info("racing record")
                emitted.set()
                handler.close()
                time.sleep(0.001)

        def reconfigure():
            assert emitted.wait(timeout=10)
            for _ in range(10):
                logging.config.dictConfig(
                    {
                        "version": 1,
                        "disable_existing_loggers": False,
                        "handlers": {},
                        "loggers": {},
                        "root": {"level": "INFO"},
                    }
                )
                time.sleep(0.001)

        emitter = threading.Thread(target=spam)
        configurer = threading.Thread(target=reconfigure)
        emitter.start()
        configurer.start()
        emitter.join(timeout=20)
        configurer.join(timeout=20)
        assert not emitter.is_alive(), "logging deadlocked against dictConfig"
        assert not configurer.is_alive(), "dictConfig deadlocked against logging"
        handler.close()
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "racing record" in _log_text(tmp_path)
