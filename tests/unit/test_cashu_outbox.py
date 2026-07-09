"""Unit tests for the Cashu outbox + retry persistence path.

These cover the guarantees introduced by the refund-token persistence fix:
  * ``store_cashu_transaction`` is idempotent (no duplicate rows on retry)
  * ``store_cashu_transaction_with_retry`` retries, then spools the full
    token to a durable outbox when the DB stays unavailable
  * ``replay_cashu_outbox`` drains the outbox into the DB and is idempotent
"""
import asyncio
import json
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import routstr.core.db as db
from routstr.core.db import (
    CashuTransaction,
    create_session,
    replay_all_outbox_files,
    replay_cashu_outbox,
    store_cashu_transaction,
    store_cashu_transaction_with_retry,
)


def _in_memory_engine():
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture
async def fresh_engine(monkeypatch, tmp_path):
    """Point db.engine at an isolated in-memory DB and the outbox at a tmp file."""
    engine = _in_memory_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    monkeypatch.setattr(db, "engine", engine)
    # create_session() looks up module-global `engine` at call time.
    monkeypatch.setattr(db, "create_session", create_session, raising=False)
    outbox = tmp_path / "cashu_outbox.jsonl"
    monkeypatch.setenv("CASHU_OUTBOX_PATH", str(outbox))
    try:
        yield engine, outbox
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_is_idempotent_on_duplicate(fresh_engine):
    """Inserting the same (token, type) twice yields exactly one row."""
    _, _ = fresh_engine
    ok1 = await store_cashu_transaction(
        token="cashuAtokenA", amount=100, unit="sat", typ="out", request_id="req-1"
    )
    ok2 = await store_cashu_transaction(
        token="cashuAtokenA", amount=100, unit="sat", typ="out", request_id="req-1"
    )
    assert ok1 is True
    assert ok2 is True  # duplicate is treated as success, not a new row

    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert len(rows) == 1
    assert rows[0].token == "cashuAtokenA"


@pytest.mark.asyncio
async def test_with_retry_succeeds_first_try(fresh_engine):
    _, _ = fresh_engine
    ok = await store_cashu_transaction_with_retry(
        token="cashuAtokenB", amount=50, unit="sat", typ="out", request_id="req-2"
    )
    assert ok is True
    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_transient_failure(fresh_engine, monkeypatch):
    """First attempt fails, second succeeds; no outbox spool, one row."""
    _, _ = fresh_engine
    calls = {"n": 0}
    real_store = store_cashu_transaction

    async def flaky_store(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return await real_store(*args, **kwargs)

    monkeypatch.setattr(db, "store_cashu_transaction", flaky_store)
    # zero backoff so the test is fast
    monkeypatch.setattr(db.asyncio, "sleep", lambda *_a, **_k: _asyncio_sleep_zero())

    ok = await store_cashu_transaction_with_retry(
        token="cashuAtokenC", amount=50, unit="sat", typ="out", request_id="req-3",
        max_retries=3,
    )
    assert ok is True
    assert calls["n"] == 2
    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_with_retry_spools_full_token_to_outbox_on_exhaustion(
    fresh_engine, monkeypatch
):
    """When the DB stays down, the full token is spooled to the outbox file."""
    _, outbox = fresh_engine

    async def always_fail(*args, **kwargs):
        return False

    monkeypatch.setattr(db, "store_cashu_transaction", always_fail)
    monkeypatch.setattr(db.asyncio, "sleep", lambda *_a, **_k: _asyncio_sleep_zero())

    full_token = "cashuAtokenD_super_secret_full_value_xyz"
    ok = await store_cashu_transaction_with_retry(
        token=full_token, amount=77, unit="sat", typ="out", request_id="req-4",
        max_retries=3,
    )
    assert ok is False
    assert outbox.exists()
    lines = outbox.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    # The full token must be recoverable from the outbox, not a preview.
    assert entry["token"] == full_token
    assert entry["amount"] == 77
    assert entry["request_id"] == "req-4"
    assert entry["type"] == "out"


@pytest.mark.asyncio
async def test_replay_drains_outbox_into_db(fresh_engine):
    """Outbox entries are persisted on replay and the outbox is cleared."""
    _, outbox = fresh_engine
    entry = {
        "outbox_id": "abc123",
        "queued_at": 1,
        "token": "cashuAtokenE",
        "amount": 200,
        "unit": "sat",
        "mint_url": None,
        "type": "out",
        "request_id": "req-5",
        "collected": False,
        "created_at": None,
        "source": "x-cashu",
        "api_key_hashed_key": None,
    }
    outbox.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    persisted = await replay_cashu_outbox()
    assert persisted == 1
    # outbox should now be empty (truncated)
    assert outbox.read_text(encoding="utf-8") == ""

    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert len(rows) == 1
    assert rows[0].token == "cashuAtokenE"


@pytest.mark.asyncio
async def test_replay_is_idempotent(fresh_engine):
    """Replaying an outbox whose entries already exist in the DB drops them."""
    _, outbox = fresh_engine
    # Pre-seed the DB row.
    await store_cashu_transaction(
        token="cashuAtokenF", amount=10, unit="sat", typ="out", request_id="req-6"
    )
    # Outbox references the same token.
    entry = {
        "outbox_id": "xyz",
        "queued_at": 1,
        "token": "cashuAtokenF",
        "amount": 10,
        "unit": "sat",
        "mint_url": None,
        "type": "out",
        "request_id": "req-6",
        "collected": False,
        "created_at": None,
        "source": "x-cashu",
        "api_key_hashed_key": None,
    }
    outbox.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    persisted = await replay_cashu_outbox()
    assert persisted == 1  # counted as persisted (already-present = success)
    assert outbox.read_text(encoding="utf-8") == ""

    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert len(rows) == 1  # no duplicate


@pytest.mark.asyncio
async def test_replay_keeps_entries_that_still_fail(fresh_engine, monkeypatch):
    """If replay still can't persist an entry, it stays in the outbox."""
    _, outbox = fresh_engine
    monkeypatch.setattr(db, "store_cashu_transaction", lambda *a, **k: _afail())
    monkeypatch.setattr(db.asyncio, "sleep", lambda *_a, **_k: _asyncio_sleep_zero())

    entry = {
        "outbox_id": "keepme",
        "queued_at": 1,
        "token": "cashuAtokenG",
        "amount": 5,
        "unit": "sat",
        "mint_url": None,
        "type": "out",
        "request_id": "req-7",
        "collected": False,
        "created_at": None,
        "source": "x-cashu",
        "api_key_hashed_key": None,
    }
    outbox.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    persisted = await replay_cashu_outbox()
    assert persisted == 0
    remaining = outbox.read_text(encoding="utf-8").strip().splitlines()
    assert len(remaining) == 1
    assert json.loads(remaining[0])["outbox_id"] == "keepme"


@pytest.mark.asyncio
async def test_replay_drops_corrupt_lines_and_persists_valid_ones(fresh_engine):
    """Corrupt JSONL lines are dropped; valid entries are persisted."""
    _, outbox = fresh_engine
    valid_entry = {
        "outbox_id": "good1",
        "queued_at": 1,
        "token": "cashuAtokenH",
        "amount": 300,
        "unit": "sat",
        "mint_url": None,
        "type": "out",
        "request_id": "req-8",
        "collected": False,
        "created_at": None,
        "source": "x-cashu",
        "api_key_hashed_key": None,
    }
    outbox.write_text(
        json.dumps(valid_entry) + "\n" + '{"token": "broken\n',
        encoding="utf-8",
    )

    persisted = await replay_cashu_outbox()
    assert persisted == 1
    assert outbox.read_text(encoding="utf-8") == ""

    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert len(rows) == 1
    assert rows[0].token == "cashuAtokenH"


@pytest.mark.asyncio
async def test_store_concurrent_duplicate_insert_yields_one_row(monkeypatch, tmp_path):
    """Two concurrent stores of the same (token, type) yield one row, both True."""
    db_file = tmp_path / "concurrent.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "create_session", create_session, raising=False)
    monkeypatch.setenv("CASHU_OUTBOX_PATH", str(tmp_path / "outbox.jsonl"))

    try:
        results = await asyncio.gather(
            store_cashu_transaction(
                token="cashuAtokenI", amount=100, unit="sat", typ="out", request_id="req-9"
            ),
            store_cashu_transaction(
                token="cashuAtokenI", amount=100, unit="sat", typ="out", request_id="req-9"
            ),
        )
        assert all(results)
        async with create_session() as session:
            rows = (await session.exec(select(CashuTransaction))).all()
        assert len(rows) == 1
        assert rows[0].token == "cashuAtokenI"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replay_under_concurrent_replay_is_idempotent(fresh_engine):
    """Two concurrent replay calls on a seeded outbox yield one row, outbox empty."""
    _, outbox = fresh_engine
    entry = {
        "outbox_id": "concurrent1",
        "queued_at": 1,
        "token": "cashuAtokenJ",
        "amount": 42,
        "unit": "sat",
        "mint_url": None,
        "type": "out",
        "request_id": "req-10",
        "collected": False,
        "created_at": None,
        "source": "x-cashu",
        "api_key_hashed_key": None,
    }
    outbox.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    results = await asyncio.gather(replay_cashu_outbox(), replay_cashu_outbox())
    # At least one call persisted the entry; both may report success.
    assert sum(results) >= 1
    assert outbox.read_text(encoding="utf-8") == ""
    async with create_session() as session:
        rows = (await session.exec(select(CashuTransaction))).all()
    assert len(rows) == 1
    assert rows[0].token == "cashuAtokenJ"


@pytest.mark.asyncio
async def test_replay_all_drains_orphaned_outbox_files(monkeypatch, tmp_path):
    """replay_all_outbox_files drains every per-PID outbox file, including orphans."""
    engine = _in_memory_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "create_session", create_session, raising=False)
    # No CASHU_OUTBOX_PATH override; _cashu_outbox_path resolves into tmp_path.
    monkeypatch.delenv("CASHU_OUTBOX_PATH", raising=False)
    monkeypatch.setattr(
        db, "_cashu_outbox_path",
        lambda: tmp_path / f"cashu_outbox.{os.getpid()}.jsonl",
    )

    def _make_entry(token: str, oid: str) -> dict:
        return {
            "outbox_id": oid,
            "queued_at": 1,
            "token": token,
            "amount": 100,
            "unit": "sat",
            "mint_url": None,
            "type": "out",
            "request_id": f"req-{oid}",
            "collected": False,
            "created_at": None,
            "source": "x-cashu",
            "api_key_hashed_key": None,
        }

    orphan_a = tmp_path / "cashu_outbox.11111.jsonl"
    orphan_b = tmp_path / "cashu_outbox.22222.jsonl"
    orphan_a.write_text(json.dumps(_make_entry("cashuOrphanA", "a1")) + "\n", encoding="utf-8")
    orphan_b.write_text(json.dumps(_make_entry("cashuOrphanB", "b2")) + "\n", encoding="utf-8")

    try:
        total = await replay_all_outbox_files()
        assert total == 2
        assert orphan_a.read_text(encoding="utf-8") == ""
        assert orphan_b.read_text(encoding="utf-8") == ""
        async with create_session() as session:
            rows = (await session.exec(select(CashuTransaction))).all()
        tokens = {r.token for r in rows}
        assert tokens == {"cashuOrphanA", "cashuOrphanB"}
    finally:
        await engine.dispose()


# --- tiny async helpers (can't use `await` inside lambdas) ---


async def _asyncio_sleep_zero():
    """No-op async sleep replacement for fast tests."""
    return None


async def _afail():
    return False
