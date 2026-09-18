from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

import routstr.auth as auth_module
import routstr.core.terminal_outcomes as outcomes_module
from routstr.auth import get_reservation_snapshot, pay_for_request
from routstr.core.db import ApiKey, ReservationRelease
from routstr.core.terminal_outcomes import TerminalOutcomeContext
from routstr.upstream.ehbp import (
    EHBPForwardingTarget,
    _context_with_served_model,
    _inject_cost_response_headers,
    finalize_ehbp_actual_cost_payment,
    finalize_ehbp_max_cost_payment,
    forward_ehbp_x_cashu_request,
)
from routstr.upstream.tinfoil_trailer import TrailerResponse


def _make_engine() -> AsyncEngine:
    return create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def test_unresolved_served_model_does_not_publish_requested_identity() -> None:
    context = TerminalOutcomeContext("ehbp-unknown-model", "requested/model")
    cost_info = {"actual_model_unresolved": True}

    resolved = _context_with_served_model(context, cost_info)

    assert resolved.model_identifier is None
    assert cost_info == {}


@pytest.fixture
async def session(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncSession, None]:
    monkeypatch.setattr("routstr.upstream.ehbp.ROUTSTR_FEE_PERCENT", 0)
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    db_session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db_session
    finally:
        for release_id in list(auth_module._reservation_heartbeats):
            await auth_module._stop_reservation_heartbeat(release_id)
        await db_session.close()
        await engine.dispose()


async def _api_key(session: AsyncSession, hashed_key: str) -> ApiKey | None:
    return (
        await session.exec(select(ApiKey).where(ApiKey.hashed_key == hashed_key))
    ).one_or_none()


def _fail_nth_api_key_update(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    target_update: int,
) -> None:
    """Return rowcount=0 for one API-key UPDATE without mutating the database."""
    original_exec = session.exec
    api_key_updates = 0

    async def exec_with_failure(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal api_key_updates
        table = getattr(statement, "table", None)
        if getattr(table, "name", None) == "api_keys":
            api_key_updates += 1
            if api_key_updates == target_update:
                return MagicMock(rowcount=0)
        return await original_exec(statement, *args, **kwargs)

    monkeypatch.setattr(session, "exec", exec_with_failure)


@pytest.mark.asyncio
async def test_finalize_actual_cost_payment_updates_balance_and_releases_reserve(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ApiKey(hashed_key="ehbp-actual", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)
    record_outcome = MagicMock()
    monkeypatch.setattr("routstr.upstream.ehbp.record_terminal_outcome", record_outcome)
    terminal_outcome = TerminalOutcomeContext(
        outcome_id="ehbp-actual-outcome",
        model_identifier="tinfoil/model",
    )

    charged = await finalize_ehbp_actual_cost_payment(
        key,
        session,
        reserved_cost_for_model=3_000,
        model_id="tinfoil/model",
        cost_info={
            "total_msats": 1_200,
            "input_tokens": 10,
            "output_tokens": 20,
            "input_msats": 500,
            "output_msats": 700,
            "input_observed": True,
            "output_observed": True,
            "cache_read_observed": True,
            "cache_creation_observed": False,
        },
        reservation_snapshot=reservation,
        terminal_outcome=terminal_outcome,
    )

    assert charged == 1_200
    updated = await _api_key(session, "ehbp-actual")
    assert updated is not None
    assert updated.balance == 8_800
    assert updated.reserved_balance == 0
    assert updated.reserved_at is None
    assert updated.total_spent == 1_200
    record_outcome.assert_called_once_with(
        TerminalOutcomeContext(
            outcome_id="ehbp-actual-outcome",
            model_identifier="tinfoil/model",
            pricing_source="missing",
            input_source="reported",
            output_source="reported",
            cache_read_source="reported",
            cache_creation_source="missing",
            input_observed=True,
            output_observed=True,
            cache_read_observed=True,
            cache_creation_observed=False,
        ),
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=1_200,
    )


@contextmanager
def _capture_payments_logs() -> Iterator[list[logging.LogRecord]]:
    """Collect ``routstr.payments`` records for the duration of the block.

    ``setup_logging()`` sets ``propagate=False`` on the ``routstr`` logger, so
    pytest's ``caplog`` (attached at the root) never sees these records; a
    handler on the payments logger itself does.
    """
    records: list[logging.LogRecord] = []

    class _RecordingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    payments_logger = logging.getLogger("routstr.payments")
    handler = _RecordingHandler(level=logging.INFO)
    previous_level = payments_logger.level
    payments_logger.addHandler(handler)
    payments_logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        payments_logger.removeHandler(handler)
        payments_logger.setLevel(previous_level)


@pytest.mark.asyncio
async def test_finalize_actual_cost_payment_logs_cache_tokens(
    session: AsyncSession,
) -> None:
    """The FINALIZE event carries the cache splits, not just input/output."""
    key = ApiKey(hashed_key="ehbp-cache-logging", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)

    with _capture_payments_logs() as records:
        charged = await finalize_ehbp_actual_cost_payment(
            key,
            session,
            reserved_cost_for_model=3_000,
            model_id="tinfoil/glm-5-2",
            cost_info={
                "total_msats": 1_200,
                "input_tokens": 5,
                "output_tokens": 20,
                "input_msats": 500,
                "output_msats": 700,
                "cache_read_input_tokens": 64,
                "cache_creation_input_tokens": 0,
                "cache_read_msats": 12,
                "cache_creation_msats": 0,
            },
            reservation_snapshot=reservation,
        )

    assert charged == 1_200
    finalize_records = [
        record for record in records if record.getMessage() == "FINALIZE"
    ]
    assert len(finalize_records) == 1
    record = finalize_records[0]
    # finalize_type/input_tokens/... are attached via logging's extra= payload.
    assert record.finalize_type == "ehbp_usage"  # type: ignore[attr-defined]
    assert record.input_tokens == 5  # type: ignore[attr-defined]
    assert record.output_tokens == 20  # type: ignore[attr-defined]
    assert record.cache_read_input_tokens == 64  # type: ignore[attr-defined]
    assert record.cache_creation_input_tokens == 0  # type: ignore[attr-defined]
    assert record.cache_read_msats == 12  # type: ignore[attr-defined]
    assert record.cache_creation_msats == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_finalize_actual_cost_payment_logs_zero_cache_when_absent(
    session: AsyncSession,
) -> None:
    """Providers that report no cache split still emit a stable key set."""
    key = ApiKey(hashed_key="ehbp-no-cache-logging", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)

    with _capture_payments_logs() as records:
        await finalize_ehbp_actual_cost_payment(
            key,
            session,
            reserved_cost_for_model=3_000,
            model_id="tinfoil/glm-5-2",
            cost_info={
                "total_msats": 1_200,
                "input_tokens": 10,
                "output_tokens": 20,
            },
            reservation_snapshot=reservation,
        )

    record = next(record for record in records if record.getMessage() == "FINALIZE")
    assert record.cache_read_input_tokens == 0  # type: ignore[attr-defined]
    assert record.cache_creation_input_tokens == 0  # type: ignore[attr-defined]
    assert record.cache_read_msats == 0  # type: ignore[attr-defined]
    assert record.cache_creation_msats == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_unmeasured_ehbp_releases_reservation(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ApiKey(hashed_key="ehbp-key", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)
    record_outcome = MagicMock()
    monkeypatch.setattr("routstr.upstream.ehbp.record_terminal_outcome", record_outcome)
    terminal_outcome = TerminalOutcomeContext(
        outcome_id="ehbp-unmeasured-outcome",
        model_identifier="tinfoil/model",
    )

    charged = await finalize_ehbp_max_cost_payment(
        key,
        session,
        max_cost_for_model=3_000,
        model_id="tinfoil/model",
        reservation_snapshot=reservation,
        terminal_outcome=terminal_outcome,
    )

    assert charged == 0
    updated = await _api_key(session, "ehbp-key")
    assert updated is not None
    assert updated.balance == 10_000
    assert updated.reserved_balance == 0
    assert updated.reserved_at is None
    assert updated.total_spent == 0
    record_outcome.assert_called_once_with(
        TerminalOutcomeContext(
            outcome_id="ehbp-unmeasured-outcome",
            model_identifier="tinfoil/model",
            input_source="missing",
            output_source="missing",
            cache_read_source="missing",
            cache_creation_source="missing",
            input_observed=False,
            output_observed=False,
            cache_read_observed=False,
            cache_creation_observed=False,
        ),
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        revenue_msats=0,
    )


@pytest.mark.asyncio
async def test_finalize_actual_cost_payment_rolls_back_when_billing_key_update_matches_no_rows(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ApiKey(hashed_key="ehbp-failed-billing-update", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)
    _fail_nth_api_key_update(session, monkeypatch, target_update=1)
    rollback_spy = AsyncMock(wraps=session.rollback)
    monkeypatch.setattr(session, "rollback", rollback_spy)

    charged = await finalize_ehbp_actual_cost_payment(
        key,
        session,
        reserved_cost_for_model=3_000,
        model_id="tinfoil/model",
        cost_info={"total_msats": 1_200},
        reservation_snapshot=reservation,
    )

    assert charged == 0
    rollback_spy.assert_awaited_once()
    updated = await _api_key(session, "ehbp-failed-billing-update")
    assert updated is not None
    assert updated.balance == 10_000
    assert updated.reserved_balance == 0
    assert updated.total_spent == 0
    release = await session.get(ReservationRelease, reservation.release_id)
    assert release is not None
    assert release.status == "released"
    assert reservation.release_id not in auth_module._reservation_heartbeats


@pytest.mark.asyncio
async def test_unmeasured_ehbp_release_is_safe_when_charge_update_would_fail(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ApiKey(hashed_key="ehbp-rollback-key", balance=10_000)
    session.add(key)
    await session.commit()
    await pay_for_request(key, 3_000, session)
    reservation = await get_reservation_snapshot(key, session)
    _fail_nth_api_key_update(session, monkeypatch, target_update=1)

    charged = await finalize_ehbp_max_cost_payment(
        key,
        session,
        max_cost_for_model=3_000,
        model_id="tinfoil/model",
        reservation_snapshot=reservation,
    )

    assert charged == 0
    updated = await _api_key(session, "ehbp-rollback-key")
    assert updated is not None
    assert updated.balance == 10_000
    # The injected partial-update failure rolls aggregate subtraction back;
    # terminal fencing prevents a charge or retry from consuming those funds.
    assert updated.reserved_balance == 3_000
    assert updated.total_spent == 0
    release = await session.get(ReservationRelease, reservation.release_id)
    assert release is not None and release.status == "released"
    assert reservation.release_id not in auth_module._reservation_heartbeats


def test_zero_debit_ehbp_headers_preserve_computed_cost() -> None:
    headers: dict[str, str] = {}

    _inject_cost_response_headers(
        headers,
        {
            "total_msats": 0,
            "computed_msats": 1_500,
            "input_msats": 1_200,
            "output_msats": 300,
        },
    )

    assert headers["X-Routstr-Cost-Msats"] == "0"
    assert headers["X-Routstr-Computed-Cost-Msats"] == "1500"


@pytest.mark.asyncio
async def test_x_cashu_ledger_failure_cannot_trigger_full_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingWriter:
        def __init__(self) -> None:
            self.losses: list[str] = []
            self.submissions: list[object] = []

        def submit(self, outcome: object) -> bool:
            self.submissions.append(outcome)
            raise RuntimeError("ledger unavailable")

        def declare_loss(self, reason: str, lost_day: object = None) -> None:
            self.losses.append(reason)

    writer = FailingWriter()
    monkeypatch.setattr(outcomes_module, "terminal_outcome_writer", writer)
    monkeypatch.setattr(
        "routstr.upstream.ehbp.recieve_token",
        AsyncMock(return_value=(10, "sat", "https://mint.example")),
    )
    monkeypatch.setattr("routstr.upstream.ehbp.store_cashu_transaction", AsyncMock())
    monkeypatch.setattr(
        "routstr.upstream.ehbp.forward_with_trailer",
        AsyncMock(
            return_value=TrailerResponse(
                status_code=200,
                headers=[],
                body=b"encrypted-response",
            )
        ),
    )
    monkeypatch.setattr(
        "routstr.upstream.ehbp._compute_ehbp_actual_cost",
        AsyncMock(
            return_value={
                "total_msats": 1_999,
                "input_tokens": 10,
                "output_tokens": 5,
                "input_msats": 1_000,
                "output_msats": 999,
                "input_observed": True,
                "output_observed": True,
                "cache_read_observed": False,
                "cache_creation_observed": False,
                "actual_model": "served-model",
                "actual_model_identifier": "served/canonical-model",
            }
        ),
    )
    send_refund = AsyncMock(return_value="cashu-refund")
    monkeypatch.setattr("routstr.upstream.ehbp.send_cashu_refund", send_refund)

    request = MagicMock()
    request.state = SimpleNamespace(request_id="ehbp-xcashu-ledger-failure")
    request.headers = {}
    request.method = "POST"
    request.query_params = {}
    request.body = AsyncMock(return_value=b"encrypted-request")
    upstream = MagicMock()
    upstream.provider_type = "tinfoil"
    upstream.prepare_headers.return_value = {}
    upstream.get_ehbp_forwarding_target.return_value = EHBPForwardingTarget(
        url="https://enclave.tinfoil.sh/v1/chat/completions"
    )
    upstream.get_confidential_inference_profile.return_value = None
    upstream.prepare_params.return_value = {}
    model = MagicMock()
    model.id = "tinfoil/model"
    model.canonical_slug = "author/model"

    response = await forward_ehbp_x_cashu_request(
        request=request,
        x_cashu_token="cashu-input",
        path="v1/chat/completions",
        max_cost_for_model=10_000,
        model_obj=model,
        upstream=upstream,
    )

    assert response.status_code == 200
    assert response.headers["x-cashu"] == "cashu-refund"
    send_refund.assert_awaited_once_with(
        8,
        "sat",
        "https://mint.example",
        "ehbp-xcashu-ledger-failure",
    )
    assert writer.losses == ["terminal outcome submission raised"]
    assert len(writer.submissions) == 1
    submission = writer.submissions[0]
    assert getattr(submission, "model_identifier") == "served/canonical-model"
    assert getattr(submission, "revenue_msats") == 2_000
    assert getattr(submission, "input_observed") is True
    assert getattr(submission, "output_observed") is True
