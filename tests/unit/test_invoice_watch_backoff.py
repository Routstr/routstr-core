"""Tests for invoice watcher poll backoff."""

from itertools import cycle, islice
from typing import cast
from unittest.mock import MagicMock

from routstr.core.db import LightningInvoice
from routstr.lightning import (
    INVOICE_POLL_MAX_INTERVAL_SECONDS,
    INVOICE_WATCH_INTERVAL_SECONDS,
    SETTLEMENT_POLL_MAX_INTERVAL_SECONDS,
    _invoice_poll_due,
    _invoice_poll_interval,
)


def _invoice(created_at: int) -> LightningInvoice:
    invoice = MagicMock()
    invoice.created_at = created_at
    return cast(LightningInvoice, invoice)


def test_poll_interval_backs_off_at_each_threshold() -> None:
    assert _invoice_poll_interval(59) == 10
    assert _invoice_poll_interval(60) == 30
    assert _invoice_poll_interval(299) == 30
    assert _invoice_poll_interval(300) == 120
    assert _invoice_poll_interval(1799) == 120
    assert _invoice_poll_interval(1800) == 600


def test_fresh_invoice_is_polled_every_cycle() -> None:
    now = 1_000_000
    for age in range(0, 60, INVOICE_WATCH_INTERVAL_SECONDS):
        prev_now = now - INVOICE_WATCH_INTERVAL_SECONDS
        assert (
            _invoice_poll_due(
                _invoice(now - age), now, prev_now, INVOICE_POLL_MAX_INTERVAL_SECONDS
            )
            is True
        )


def test_future_created_at_is_polled_immediately() -> None:
    now = 1_000_000
    assert (
        _invoice_poll_due(
            _invoice(now + 500), now, now - 10, INVOICE_POLL_MAX_INTERVAL_SECONDS
        )
        is True
    )


def test_settlement_pending_polls_at_its_capped_interval() -> None:
    # Confirmed-paid rows owe a credit, so their backoff stops at 60s instead of
    # decaying to the 600s bucket a day-old unpaid invoice would reach.
    now = 100_000
    invoices = [_invoice(now - 86_400 - offset) for offset in range(600)]
    due = sum(
        1
        for inv in invoices
        if _invoice_poll_due(inv, now, now - 10, SETTLEMENT_POLL_MAX_INTERVAL_SECONDS)
    )
    assert due == 100


def test_aged_invoices_do_not_stampede_together() -> None:
    now = 100_000
    invoices = [_invoice(now - 86_400 - offset) for offset in range(600)]
    due = sum(
        1
        for inv in invoices
        if _invoice_poll_due(inv, now, now - 10, INVOICE_POLL_MAX_INTERVAL_SECONDS)
    )
    # One 600s bucket boundary falls inside each 10s cycle, so 600 day-old
    # invoices cost 10 mint calls per cycle instead of 600.
    assert due == 10


def test_jittered_cycle_times_still_poll_every_invoice() -> None:
    # A cycle is 10s of sleep plus however long the batch took, so `now` never
    # advances on a clean 10s grid.
    invoices = [_invoice(offset) for offset in range(600)]
    polls = [0] * len(invoices)
    start = 100_000
    now = start
    for step in islice(cycle((13, 27)), 400):
        prev_now, now = now, now + step
        for index, invoice in enumerate(invoices):
            if _invoice_poll_due(
                invoice, now, prev_now, INVOICE_POLL_MAX_INTERVAL_SECONDS
            ):
                polls[index] += 1

    elapsed = now - start
    assert min(polls) >= elapsed // 600
