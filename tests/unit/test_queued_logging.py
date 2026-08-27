import logging
import threading
from pathlib import Path

import routstr.core.logging as routstr_logging
from routstr.core.logging import QueuedDailyRotatingFileHandler


def test_queued_file_handler_flushes_records_on_close(tmp_path: Path) -> None:
    handler = QueuedDailyRotatingFileHandler(
        str(tmp_path / "app.log"), when="midnight", backupCount=1
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.Logger("queued-file-test")
    logger.addHandler(handler)
    logger.info("written from listener")
    handler.flush()

    log_path = next(tmp_path.glob("app_*.log"))
    assert "written from listener" in log_path.read_text()

    handler.close()


def test_queued_file_handler_keeps_logging_after_close(tmp_path: Path) -> None:
    """dictConfig closes live handlers; uvicorn runs one after app import."""
    handler = QueuedDailyRotatingFileHandler(
        str(tmp_path / "app.log"), when="midnight", backupCount=1
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.Logger("queued-file-reopen-test")
    logger.addHandler(handler)
    logger.info("before close")
    handler.close()
    assert handler._closed

    logging._handlerList[:] = [
        reference for reference in logging._handlerList if reference() is not handler
    ]
    logger.info("after close")
    handler.flush()

    assert any(reference() is handler for reference in logging._handlerList)

    log_path = next(tmp_path.glob("app_*.log"))
    assert "after close" in log_path.read_text()

    handler.close()


def test_queued_file_handler_contains_reopen_failures(
    tmp_path: Path, monkeypatch
) -> None:
    handler = QueuedDailyRotatingFileHandler(
        str(tmp_path / "app.log"), when="midnight", backupCount=1
    )
    logger = logging.Logger("queued-file-failure-test")
    logger.addHandler(handler)
    handler.close()

    def fail_to_open(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(routstr_logging, "DailyRotatingFileHandler", fail_to_open)
    previous = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        logger.info("must not reach billing")
    finally:
        logging.raiseExceptions = previous
        handler.close()


def test_queued_file_handler_survives_close_racing_with_emit(tmp_path: Path) -> None:
    """Closing while another thread logs must not hang or orphan the queue."""
    handler = QueuedDailyRotatingFileHandler(
        str(tmp_path / "app.log"), when="midnight", backupCount=1
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.Logger("queued-file-race-test")
    logger.addHandler(handler)

    done = threading.Event()

    def spam() -> None:
        while not done.is_set():
            logger.info("racing record")

    def churn() -> None:
        for _ in range(50):
            handler.close()

    emitter = threading.Thread(target=spam)
    closer = threading.Thread(target=churn)
    emitter.start()
    closer.start()

    closer.join(timeout=10)
    done.set()
    emitter.join(timeout=10)

    assert not closer.is_alive(), "close() deadlocked against a concurrent emit()"
    assert not emitter.is_alive(), "emit() deadlocked against a concurrent close()"

    logger.info("final record")
    handler.flush()

    log_path = next(tmp_path.glob("app_*.log"))
    assert "final record" in log_path.read_text()

    handler.close()
