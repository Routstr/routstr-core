"""Fallback settlement must not turn missing outcomes into clean coverage."""

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, NoReturn, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from routstr.auth import ReservationSnapshot
from routstr.core.db import ApiKey
from routstr.core.terminal_outcomes import TerminalOutcomeContext
from routstr.upstream.base import BaseUpstreamProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses", "messages", "generic"])
@pytest.mark.parametrize("failure", ["missing_key", "lookup", "adjustment"])
async def test_disconnected_fallback_failure_records_unknown(
    monkeypatch: pytest.MonkeyPatch, api: str, failure: str
) -> None:
    provider = BaseUpstreamProvider(base_url="https://example.com", api_key="test")
    key = MagicMock(spec=ApiKey)
    key.hashed_key = "stream-key"
    key.balance = 1000
    snapshot = ReservationSnapshot(
        release_id="stream-release",
        key_hash=key.hashed_key,
        billing_key_hash=key.hashed_key,
        reserved_msats=500,
    )
    context = TerminalOutcomeContext(outcome_id="stream", model_identifier="model")
    session = MagicMock()
    session.get = AsyncMock(
        return_value=None if failure == "missing_key" else key,
        side_effect=RuntimeError("lookup failed") if failure == "lookup" else None,
    )
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    adjust = AsyncMock(side_effect=RuntimeError("settlement failed"))
    monkeypatch.setattr(
        provider,
        "_release_failed_streaming_reservation",
        AsyncMock(return_value=True),
    )

    async def chunks() -> AsyncGenerator[bytes, None]:
        yield b"data: ignored\n\n"

    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {"content-type": "text/event-stream"}
    response.aiter_bytes = chunks
    response.aclose = AsyncMock()
    with (
        patch("routstr.upstream.base.create_session", return_value=session_context),
        patch("routstr.upstream.base.adjust_payment_for_tokens", adjust),
        patch("routstr.upstream.base.mark_terminal_outcome_unknown") as loss,
    ):
        try:
            if api == "generic":
                await provider._finalize_generic_streaming_payment(
                    key.hashed_key, 500, "/opaque", None, None, snapshot, context
                )
            else:
                handler = getattr(provider, f"handle_streaming_{api}_completion")
                streaming = await handler(
                    response,
                    key,
                    500,
                    reservation_snapshot=snapshot,
                    terminal_outcome=context,
                )
                # The client disconnects before consuming the first byte.
                await cast(
                    AsyncGenerator[bytes, None], streaming.body_iterator
                ).aclose()
        except RuntimeError:
            pytest.fail("accounted fallback failure must not escape to finalizer guard")
        assert loss.called
        assert loss.call_args.args[0] is context


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat", "responses", "messages", "generic"])
@pytest.mark.parametrize("failure", ["ambiguous", "lookup"])
async def test_ambiguous_settlement_unknown_is_not_promoted_to_day_loss(
    monkeypatch: pytest.MonkeyPatch,
    api: str,
    failure: str,
) -> None:
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel, select
    from sqlmodel.ext.asyncio.session import AsyncSession

    from routstr.core import terminal_outcomes
    from routstr.core.db import TerminalOutcome, TerminalOutcomeEpoch

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    writer = terminal_outcomes.TerminalOutcomeWriter(session_factory=sessions)
    monkeypatch.setattr(terminal_outcomes, "terminal_outcome_writer", writer)
    assert await writer.start()
    context = TerminalOutcomeContext("ambiguous-stream", "model")
    key = MagicMock(spec=ApiKey)
    key.hashed_key = "key"
    key.balance = 1000
    session = MagicMock()
    session.get = AsyncMock(
        return_value=key,
        side_effect=RuntimeError("lookup failed") if failure == "lookup" else None,
    )
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    snapshot = ReservationSnapshot(
        release_id="release", key_hash="key", billing_key_hash="key", reserved_msats=500
    )

    async def ambiguous(*args: Any, **kwargs: Any) -> NoReturn:
        # _commit_settlement already emits this placeholder before re-raising.
        terminal_outcomes.mark_terminal_outcome_unknown(
            context, "prepaid_commit_ambiguous"
        )
        raise RuntimeError("commit acknowledgement lost")

    provider = BaseUpstreamProvider(base_url="https://example.com", api_key="test")
    monkeypatch.setattr(
        provider,
        "_release_failed_streaming_reservation",
        AsyncMock(return_value=True),
    )

    async def chunks() -> AsyncGenerator[bytes, None]:
        yield b"data: ignored\n\n"

    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {"content-type": "text/event-stream"}
    response.aiter_bytes = chunks
    response.aclose = AsyncMock()
    try:
        with (
            patch("routstr.upstream.base.create_session", return_value=session_context),
            patch("routstr.upstream.base.adjust_payment_for_tokens", ambiguous),
        ):
            if api == "generic":
                await provider._finalize_generic_streaming_payment(
                    "key", 500, "/opaque", None, None, snapshot, context
                )
            else:
                handler = getattr(provider, f"handle_streaming_{api}_completion")
                streaming = await handler(
                    response,
                    key,
                    500,
                    reservation_snapshot=snapshot,
                    terminal_outcome=context,
                )
                await cast(
                    AsyncGenerator[bytes, None], streaming.body_iterator
                ).aclose()
        assert await writer.flush(timeout=1)
        assert not writer.loss_pending
        async with sessions() as db_session:
            rows = (await db_session.exec(select(TerminalOutcome))).all()
            epochs = (await db_session.exec(select(TerminalOutcomeEpoch))).all()
        assert len(rows) == 1
        assert rows[0].pricing_source == "unknown"
        assert rows[0].revenue_msats == 0
        assert len(epochs) == 1 and epochs[0].coverage_end_day is None
    finally:
        await writer.stop(timeout=1)
        await engine.dispose()
