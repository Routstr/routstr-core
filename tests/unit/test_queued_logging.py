import logging
from pathlib import Path

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
