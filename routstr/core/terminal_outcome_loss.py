"""Crash-durable collection-loss evidence, not a spool of request outcomes."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import AsyncContextManager

from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from .db import TerminalOutcomeWriterRun


class LossJournal:
    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory

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

    def record(self, lost_day: date, detected_at_ms: int) -> None:
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
            marker_id = uuid.UUID(hex=marker.stem).hex
            if marker_id != marker.stem:
                raise ValueError("Invalid terminal loss marker name")
            try:
                evidence = json.loads(marker.read_text())
            except FileNotFoundError:
                continue  # A sibling already committed and removed this marker.
            day = date.fromisoformat(evidence["loss_day"])
            detected = evidence["detected_at_ms"]
            if type(detected) is not int or detected < 0:
                raise ValueError("Invalid terminal loss marker timestamp")
            run_id = f"loss-{marker_id}"
            async with session_factory() as session:
                existing = await session.get(TerminalOutcomeWriterRun, run_id)
                if existing is None:
                    session.add(
                        TerminalOutcomeWriterRun(
                            run_id=run_id,
                            status="recovered",
                            loss_day=day,
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
