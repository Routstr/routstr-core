from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from routstr.core.db import TerminalOutcomeWriterRun
from routstr.core.terminal_outcome_loss import LossJournal


@pytest.mark.asyncio
async def test_rejection_survives_immediate_exit_and_concurrent_replay(
    analytics_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "crash-journal"
    code = """
import os
from datetime import date
from routstr.core.terminal_outcome_writer import TerminalOutcomeWriter, _QueuedOutcome
writer = TerminalOutcomeWriter()
assert not writer.submit(_QueuedOutcome(
    outcome_id="not-persisted", model_identifier="never-log-this", pricing_source=None,
    served_model_identifier=None,
    terminal_at_ms=1000, terminal_day=date(2026, 9, 1), input_tokens=1,
    output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
    revenue_msats=0, input_source="reported", output_source="missing",
    cache_read_source="missing", cache_creation_source="missing"))
os._exit(0)
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code],
        env={**os.environ, "TERMINAL_OUTCOME_LOSS_DIR": str(directory)},
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    markers = list(directory.glob("*.json"))
    assert len(markers) == 1
    evidence = json.loads(markers[0].read_text())
    assert set(evidence) == {"loss_day", "detected_at_ms", "loss_reason"}
    assert evidence["loss_reason"] == "outcome_dropped"
    assert "never-log-this" not in markers[0].read_text()
    sessions = async_sessionmaker(
        analytics_engine, class_=AsyncSession, expire_on_commit=False
    )
    journal = LossJournal(directory)
    await asyncio.gather(
        journal.replay(sessions, evidence["detected_at_ms"]),
        journal.replay(sessions, evidence["detected_at_ms"]),
    )
    await journal.replay(sessions, evidence["detected_at_ms"])
    async with sessions() as session:
        runs = (await session.exec(select(TerminalOutcomeWriterRun))).all()
    assert len(runs) == 1 and runs[0].status == "recovered"
    assert runs[0].loss_day == date(2026, 9, 1)
    assert runs[0].loss_reason == "outcome_dropped"
    assert not list(directory.glob("*.json"))


@pytest.mark.asyncio
async def test_marker_written_before_loss_reasons_replays_without_one(
    analytics_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    marker = tmp_path / f"{'a' * 32}.json"
    marker.write_text(json.dumps({"loss_day": "2026-09-01", "detected_at_ms": 1000}))
    sessions = async_sessionmaker(
        analytics_engine, class_=AsyncSession, expire_on_commit=False
    )
    await LossJournal(tmp_path).replay(sessions, 2000)
    async with sessions() as session:
        run = (await session.exec(select(TerminalOutcomeWriterRun))).one()
    assert run.loss_day == date(2026, 9, 1)
    assert run.loss_reason is None


@pytest.mark.asyncio
async def test_replay_keeps_marker_until_commit_and_is_idempotent_after_unlink_failure(
    analytics_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = LossJournal(tmp_path / "journal")
    journal.record(date(2026, 9, 1), 1000)

    @asynccontextmanager
    async def offline() -> AsyncIterator[AsyncSession]:
        raise ConnectionError("database unavailable")
        yield

    with pytest.raises(ConnectionError):
        await journal.replay(offline, 2000)
    assert len(list(journal.directory.glob("*.json"))) == 1
    sessions = async_sessionmaker(
        analytics_engine, class_=AsyncSession, expire_on_commit=False
    )
    unlink = Path.unlink

    def fail_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.suffix == ".json":
            raise OSError("synthetic crash after commit")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(OSError):
            await journal.replay(sessions, 2000)
    await journal.replay(sessions, 3000)
    async with sessions() as session:
        runs = (await session.exec(select(TerminalOutcomeWriterRun))).all()
    assert len(runs) == 1 and runs[0].closed_at_ms == 2000
    assert not list(journal.directory.glob("*.json"))


def test_marker_syncs_file_and_directory_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = LossJournal(tmp_path)
    synced = []
    original = os.fsync

    def fsync(descriptor: int) -> None:
        synced.append(descriptor)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    journal.record(date(2026, 9, 1), 1000)
    assert len(synced) == 2
    assert not list(tmp_path.glob("*.tmp"))
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_journal_failure_is_not_silently_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routstr.core.terminal_outcome_writer import TerminalOutcomeWriter

    writer = TerminalOutcomeWriter(loss_directory=tmp_path)

    def broken(*args: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(writer._loss_journal, "record", broken)
    with pytest.raises(OSError):
        writer.declare_loss("rejected", date(2026, 9, 1))
    assert writer.loss_pending
    assert writer._journaled_loss_day is None
