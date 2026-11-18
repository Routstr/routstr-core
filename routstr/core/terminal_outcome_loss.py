"""Crash-durable collection-loss evidence, not a spool of request outcomes."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import AsyncContextManager

from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from .db import TerminalOutcomeWriterRun
from .logging import get_logger

logger = get_logger(__name__)

UNREADABLE_MARKER_REASON = "unreadable loss marker"


def _marker_id(marker: Path) -> str | None:
    try:
        marker_id = uuid.UUID(hex=marker.stem).hex
    except ValueError:
        return None
    return marker_id if marker_id == marker.stem else None


def _parse_marker(text: str) -> tuple[date, int, str | None]:
    evidence = json.loads(text)
    day = date.fromisoformat(evidence["loss_day"])
    detected = evidence["detected_at_ms"]
    if type(detected) is not int or detected < 0:
        raise ValueError("Invalid terminal loss marker timestamp")
    reason = evidence.get("loss_reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("Invalid terminal loss marker reason")
    return day, detected, reason


class LossJournal:
    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory
        self._ignored: set[str] = set()

    @property
    def directory(self) -> Path:
        return self._directory or Path(
            os.environ.get("TERMINAL_OUTCOME_LOSS_DIR", "data/terminal-outcome-losses")
        )

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _mkdir(self, path: Path) -> None:
        if path.is_dir():
            return
        self._mkdir(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        self._sync_directory(path.parent)

    def record(
        self, lost_day: date, detected_at_ms: int, reason: str | None = None
    ) -> None:
        directory = self.directory
        self._mkdir(directory)
        marker_id = uuid.uuid4().hex
        temporary = directory / f"{marker_id}.tmp"
        final = directory / f"{marker_id}.json"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(
                    {
                        "loss_day": lost_day.isoformat(),
                        "detected_at_ms": detected_at_ms,
                        "loss_reason": reason,
                    },
                    output,
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, final)
            self._sync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)

    async def replay(
        self,
        session_factory: Callable[[], AsyncContextManager[AsyncSession]],
        now: int,
    ) -> None:
        directory = self.directory
        for marker in directory.glob("*.json"):
            marker_id = _marker_id(marker)
            if marker_id is None:
                # Not written by record(); leave the operator's file alone.
                if marker.name not in self._ignored:
                    self._ignored.add(marker.name)
                    logger.warning(
                        "Ignoring unexpected file in terminal loss directory",
                        extra={"file_name": marker.name},
                    )
                continue
            try:
                text = marker.read_text()
                modified_ms = int(marker.stat().st_mtime * 1000)
            except FileNotFoundError:
                continue  # A sibling already committed and removed this marker.
            try:
                day, detected, reason = _parse_marker(text)
            except (ValueError, KeyError, TypeError):
                # A loss happened but its record is damaged: keep the evidence,
                # dated by when the marker was written.
                logger.error(
                    "Terminal loss marker unreadable; recording it by file time",
                    extra={"file_name": marker.name},
                )
                detected = max(modified_ms, 0)
                day = datetime.fromtimestamp(detected / 1000, timezone.utc).date()
                reason = UNREADABLE_MARKER_REASON
            run_id = f"loss-{marker_id}"
            async with session_factory() as session:
                existing = await session.get(TerminalOutcomeWriterRun, run_id)
                if existing is None:
                    session.add(
                        TerminalOutcomeWriterRun(
                            run_id=run_id,
                            status="recovered",
                            loss_day=day,
                            # Markers written before reasons were stored have none.
                            loss_reason=reason,
                            started_at_ms=detected,
                            heartbeat_at_ms=detected,
                            closed_at_ms=max(now, detected),
                        )
                    )
                    try:
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()
                    existing = await session.get(TerminalOutcomeWriterRun, run_id)
                if (
                    existing is None
                    or existing.loss_day != day
                    or existing.started_at_ms != detected
                    or existing.status != "recovered"
                ):
                    raise RuntimeError("Terminal loss marker was not committed")
            # An interrupted unlink is safe: replay uses the marker's stable ID.
            marker.unlink(missing_ok=True)
            self._sync_directory(directory)
