from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from nostr_sdk import Event, EventBuilder, Keys, Kind, Tag, Timestamp

from routstr.nostr.analytics_v2 import (
    COLUMNS,
    DEFAULT_MAX_FRAME_BYTES,
    MAX_SAFE_INTEGER,
    AnalyticsV2Error,
    DayAggregate,
    FrameTooLargeError,
    LedgerOutcome,
    aggregate_ledger_week,
    build_analytics_address,
    encode_week_event,
    prior_version_from_frame,
)

PRIVATE_KEY = "11" * 32
WEEK = date(2026, 9, 14)


def _outcome(
    day: date = WEEK, model: str | None = "model/a", **changes: Any
) -> LedgerOutcome:
    return replace(
        LedgerOutcome(
            terminal_day=day,
            model_identifier=model,
            input_tokens=10,
            output_tokens=4,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            revenue_msats=25,
            input_source="reported",
            output_source="estimated",
            cache_read_source="missing",
            cache_creation_source="missing",
        ),
        **changes,
    )


def _aggregate(outcomes: list[LedgerOutcome] | None = None, **changes: Any) -> Any:
    args: dict[str, Any] = dict(
        epoch=0,
        epoch_coverage_start=WEEK,
        epoch_coverage_end=None,
        week=WEEK,
        today_utc=WEEK + timedelta(days=4),
    )
    args.update(changes)
    aggregate = aggregate_ledger_week(outcomes or [], **args)
    assert aggregate is not None
    return aggregate


def _encode(aggregate: Any, **changes: Any) -> Any:
    args: dict[str, Any] = dict(
        private_key_hex=PRIVATE_KEY, provider_d="provider", created_at=1789689600
    )
    args.update(changes)
    return encode_week_event(aggregate, **args)


def _payload(encoded: Any) -> dict[str, Any]:
    return json.loads(encoded.content)


def _resign(encoded: Any, payload: dict[str, Any]) -> bytes:
    event = (
        EventBuilder(
            Kind(38422),
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        )
        .tags([Tag.parse(list(tag)) for tag in encoded.tags])
        .custom_created_at(Timestamp.from_secs(encoded.created_at))
        .finalize(Keys.parse(PRIVATE_KEY))
    )
    return json.dumps(
        ["EVENT", json.loads(event.as_json())],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _assert_partitions(payload: dict[str, Any]) -> None:
    assert payload["daily_models"].keys() == payload["days"].keys()
    for day, values in payload["days"].items():
        models = payload["daily_models"][day]
        assert "_other" in models
        assert [
            sum(row[column] for row in models.values())
            for column in range(len(COLUMNS))
        ] == values
        for row in [values, *models.values()]:
            for side in range(4):
                assert row[1 + side] + row[10 + side] + row[14 + side] == row[0]
            assert row[18] <= min(row[1], row[2])
            assert row[19] <= sum(row[5:9])
            assert row[18] != 0 or row[19] == 0


def test_partial_week_has_daily_models_and_zero_days() -> None:
    rows = [
        _outcome(),
        _outcome(WEEK, "model/free", revenue_msats=0),
        _outcome(WEEK + timedelta(days=2), None),
        _outcome(WEEK + timedelta(days=4), "today/not-published"),
        _outcome(WEEK - timedelta(days=1), "before/not-published"),
    ]
    encoded = _encode(_aggregate(rows))
    payload = _payload(encoded)
    assert payload["complete"] is False
    assert payload["through"] == "2026-09-17"
    assert payload["days"]["2026-09-14"][0] == 2
    assert payload["days"]["2026-09-15"] == [0] * len(COLUMNS)
    assert payload["daily_models"]["2026-09-14"]["model/free"][9] == 0
    assert payload["daily_models"]["2026-09-16"]["_other"][0] == 1
    assert "today/not-published" not in encoded.content.decode()
    _assert_partitions(payload)
    assert prior_version_from_frame(encoded.frame) == encoded.as_prior_version()


def test_reported_estimated_and_missing_usage_stay_separate() -> None:
    payload = _payload(_encode(_aggregate([_outcome()])))
    row = payload["days"][WEEK.isoformat()]
    assert row == [1, 1, 0, 0, 0, 10, 4, 0, 0, 25, 0, 1, 0, 0, 0, 0, 1, 1, 0, 0]
    _assert_partitions(payload)


def test_measured_cohort_cannot_be_inferred_from_separate_source_counts() -> None:
    measured = _outcome(output_source="reported", output_tokens=20)
    missing = _outcome(
        input_source="missing",
        input_tokens=0,
        output_source="missing",
        output_tokens=0,
    )
    together = _aggregate([measured, missing]).days[0].values
    separate = (
        _aggregate(
            [
                replace(
                    measured,
                    output_source="missing",
                    output_tokens=0,
                ),
                replace(
                    missing,
                    output_source="reported",
                    output_tokens=20,
                ),
            ]
        )
        .days[0]
        .values
    )
    assert together[:18] == separate[:18]
    assert together[18:] == (1, 30)
    assert separate[18:] == (0, 0)


@pytest.mark.parametrize(
    ("changes", "cohort"),
    [
        ({}, (1, 30)),
        ({"input_tokens": 0, "output_tokens": 0, "revenue_msats": 0}, (1, 0)),
        ({"input_source": "estimated"}, (0, 0)),
        ({"output_source": "estimated"}, (0, 0)),
        ({"input_source": "missing"}, (0, 0)),
        ({"output_source": "missing"}, (0, 0)),
        ({"cache_read_source": "reported", "cache_read_input_tokens": 6}, (1, 36)),
        (
            {"cache_creation_source": "reported", "cache_creation_input_tokens": 4},
            (1, 34),
        ),
        ({"cache_read_source": "estimated"}, (0, 0)),
        ({"cache_creation_source": "estimated"}, (0, 0)),
        ({"cache_read_input_tokens": 6}, (0, 0)),
        ({"cache_creation_input_tokens": 4}, (0, 0)),
        ({"completed_requests": 3, "input_tokens": 30, "output_tokens": 60}, (3, 90)),
    ],
)
def test_measured_cohort_uses_matching_request_and_token_totals(
    changes: dict[str, Any], cohort: tuple[int, int]
) -> None:
    measured = _outcome(output_source="reported", output_tokens=20)
    payload = _payload(_encode(_aggregate([replace(measured, **changes)])))
    assert payload["days"][WEEK.isoformat()][18:] == list(cohort)
    _assert_partitions(payload)


@pytest.mark.parametrize("source", ["reported", "estimated", "missing"])
def test_each_usage_source_lands_in_its_own_counters(source: str) -> None:
    changes: dict[str, Any] = {
        f"{name}_source": source
        for name in ("input", "output", "cache_read", "cache_creation")
    }
    row = (
        _aggregate(
            [
                _outcome(
                    cache_read_input_tokens=6, cache_creation_input_tokens=4, **changes
                )
            ]
        )
        .days[0]
        .values
    )
    if source == "reported":
        assert row[1:5] == (1,) * 4
        assert row[18:] == (1, 24)
    elif source == "estimated":
        assert row[10:14] == (1,) * 4
        assert row[18:] == (0, 0)
    else:
        assert row[14:18] == (1,) * 4
        assert row[18:] == (0, 0)


def test_epoch_start_and_end_exclude_partial_and_unknown_days() -> None:
    aggregate = _aggregate(
        [_outcome(WEEK + timedelta(days=offset)) for offset in range(7)],
        epoch=7,
        epoch_coverage_start=WEEK + timedelta(days=1),
        epoch_coverage_end=WEEK + timedelta(days=2),
        today_utc=WEEK + timedelta(days=8),
    )
    payload = _payload(_encode(aggregate))
    assert payload["complete"] is True
    assert set(payload["days"]) == {"2026-09-15", "2026-09-16"}
    _assert_partitions(payload)
    assert (
        aggregate_ledger_week(
            [],
            epoch=0,
            epoch_coverage_start=WEEK,
            epoch_coverage_end=None,
            week=WEEK,
            today_utc=WEEK,
        )
        is None
    )


def test_week_is_transport_coordinate_not_model_reporting_resolution() -> None:
    early = _encode(_aggregate([_outcome()], epoch=1, epoch_coverage_end=WEEK))
    late = _encode(
        _aggregate(
            [_outcome(WEEK + timedelta(days=2))],
            epoch=2,
            epoch_coverage_start=WEEK + timedelta(days=2),
        )
    )
    coordinate = f"38421:{early.pubkey}:provider"
    digest = hashlib.sha256(coordinate.encode()).hexdigest()[:16]
    assert early.d_tag == f"routstr.analytics.v2:{digest}:week:2026-09-14:epoch:1"
    assert late.d_tag != early.d_tag
    assert early.tags[1] == ("a", coordinate)
    assert not (_payload(early)["days"].keys() & _payload(late)["days"].keys())


def test_corrected_closed_week_retains_daily_model_detail() -> None:
    prior = _encode(_aggregate([_outcome()], today_utc=WEEK + timedelta(days=7)))
    corrected = _encode(
        _aggregate(
            [_outcome(model="model/corrected")],
            today_utc=WEEK + timedelta(days=7),
            prior_version=prior.as_prior_version(),
            correction=True,
        ),
        created_at=prior.created_at + 1,
    )
    payload = _payload(corrected)
    assert payload["corrected"] is True
    assert payload["corrects"] == prior.event_id
    assert "model/corrected" in payload["daily_models"][WEEK.isoformat()]
    assert "model/a" not in payload["daily_models"][WEEK.isoformat()]
    _assert_partitions(payload)
    assert (
        prior_version_from_frame(corrected.frame).daily_models == corrected.daily_models
    )


def test_ordinary_successor_cannot_rewrite_model_mix_or_prior_daily_totals() -> None:
    prior = _encode(_aggregate([_outcome()], today_utc=WEEK + timedelta(days=1)))
    for changed in (_outcome(model="changed"), _outcome(revenue_msats=100)):
        aggregate = _aggregate(
            [changed],
            today_utc=WEEK + timedelta(days=2),
            prior_version=prior.as_prior_version(),
        )
        with pytest.raises(AnalyticsV2Error, match="changed a published row"):
            _encode(aggregate, created_at=prior.created_at + 1)
    unchanged = _aggregate(
        [_outcome()],
        today_utc=WEEK + timedelta(days=2),
        prior_version=prior.as_prior_version(),
    )
    assert (
        _payload(_encode(unchanged, created_at=prior.created_at + 1))["through"]
        == "2026-09-15"
    )


def test_new_epochs_do_not_accept_a_different_epoch_as_predecessor() -> None:
    prior = _encode(_aggregate([_outcome()], epoch=1, epoch_coverage_end=WEEK))
    fresh = _aggregate(
        [],
        epoch=2,
        epoch_coverage_start=WEEK + timedelta(days=2),
        prior_version=prior.as_prior_version(),
    )
    with pytest.raises(AnalyticsV2Error, match="different coordinate"):
        _encode(fresh, created_at=prior.created_at + 1)


def test_single_request_models_are_named_without_an_arbitrary_model_cap() -> None:
    aggregate = _aggregate([_outcome(model=f"model/{index}") for index in range(30)])
    payload = _payload(_encode(aggregate))
    assert len(payload["daily_models"][WEEK.isoformat()]) == 31
    _assert_partitions(payload)


def test_utf8_size_folding_preserves_every_day_and_metric() -> None:
    aggregate = _aggregate(
        [
            _outcome(
                WEEK + timedelta(days=index % 4),
                "模型/" + str(index) + "/" + "x" * 120,
                output_source="reported" if index % 3 else "estimated",
                cache_read_source="reported",
                cache_read_input_tokens=6,
                cache_creation_source="reported",
                cache_creation_input_tokens=4,
            )
            for index in range(120)
        ]
    )
    full = _encode(aggregate)
    assert len(full.frame) < DEFAULT_MAX_FRAME_BYTES
    limit = len(full.frame) // 3
    folded = _encode(aggregate, max_frame_bytes=limit)
    assert len(folded.frame) <= limit
    assert any(
        row["_other"][0] > 0 for row in _payload(folded)["daily_models"].values()
    )
    assert any(
        row["_other"][18] > 0 and row["_other"][19] > 0
        for row in _payload(folded)["daily_models"].values()
    )
    assert _payload(folded)["days"] == _payload(full)["days"]
    _assert_partitions(_payload(folded))
    assert prior_version_from_frame(folded.frame) == folded.as_prior_version()
    with pytest.raises(FrameTooLargeError):
        _encode(aggregate, max_frame_bytes=100)


def test_sdk_signature_and_exact_canonical_frame_round_trip() -> None:
    encoded = _encode(_aggregate([_outcome(model='model/雪"\\')]))
    event = encoded.event
    assert Event.from_json(json.dumps(event)).verify()
    assert (
        encoded.frame
        == json.dumps(
            ["EVENT", event], separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    assert prior_version_from_frame(encoded.frame) == encoded.as_prior_version()
    with pytest.raises(AnalyticsV2Error, match="not canonical"):
        prior_version_from_frame(json.dumps(["EVENT", event]).encode())
    for field, value in (
        ("created_at", event["created_at"] + 1),
        ("content", event["content"].replace("model/", "renamed/")),
    ):
        tampered = {**event, field: value}
        assert Event.from_json(json.dumps(tampered)).verify_signature()
        with pytest.raises(AnalyticsV2Error):
            prior_version_from_frame(
                json.dumps(
                    ["EVENT", tampered], separators=(",", ":"), ensure_ascii=False
                ).encode()
            )
    event["sig"] = "00" * 64
    with pytest.raises(AnalyticsV2Error, match="signature"):
        prior_version_from_frame(
            json.dumps(
                ["EVENT", event], separators=(",", ":"), ensure_ascii=False
            ).encode()
        )


@pytest.mark.parametrize(
    "change",
    [
        "missing_day",
        "missing_models",
        "nonpartition",
        "provenance",
        "unsafe_integer",
        "bool",
        "wrong_epoch",
        "draft_columns",
    ],
)
def test_signed_invalid_payloads_are_rejected(change: str) -> None:
    encoded = _encode(_aggregate([_outcome(output_source="reported")]))
    payload = _payload(encoded)
    day = WEEK.isoformat()
    if change == "missing_day":
        del payload["days"][day]
    elif change == "missing_models":
        del payload["daily_models"][day]
    elif change == "nonpartition":
        payload["daily_models"][day]["_other"][9] = 1
    elif change == "provenance":
        payload["days"][day][10] = 1
    elif change == "unsafe_integer":
        payload["days"][day][9] = MAX_SAFE_INTEGER + 1
    elif change == "bool":
        payload["days"][day][9] = False
    elif change == "draft_columns":
        payload["columns"] = payload["columns"][:18]
        payload["days"] = {day: values[:18] for day, values in payload["days"].items()}
        payload["daily_models"] = {
            day: {model: values[:18] for model, values in models.items()}
            for day, models in payload["daily_models"].items()
        }
    else:
        payload["epoch"] = 42
    with pytest.raises(AnalyticsV2Error):
        prior_version_from_frame(_resign(encoded, payload))


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        (18, 2, "Measured requests"),
        (19, 15, "Measured tokens"),
        (18, 0, "Measured tokens"),
    ],
)
def test_signed_invalid_measured_cohorts_are_rejected_even_when_models_partition(
    column: int, value: int, message: str
) -> None:
    encoded = _encode(_aggregate([_outcome(output_source="reported")]))
    payload = _payload(encoded)
    day = WEEK.isoformat()
    payload["days"][day][column] = value
    payload["daily_models"][day]["model/a"][column] = value
    with pytest.raises(AnalyticsV2Error, match=message):
        prior_version_from_frame(_resign(encoded, payload))


def test_aggregate_rejects_unsafe_values_and_invalid_model_partition() -> None:
    with pytest.raises(AnalyticsV2Error):
        _aggregate([_outcome(revenue_msats=MAX_SAFE_INTEGER + 1)])
    with pytest.raises(AnalyticsV2Error):
        _aggregate([_outcome(input_source="guessed")])
    aggregate = _aggregate([_outcome()])
    broken = replace(
        aggregate, days=(DayAggregate(WEEK, (0,) * len(COLUMNS)), *aggregate.days[1:])
    )
    with pytest.raises(AnalyticsV2Error, match="partition"):
        _encode(broken)


@pytest.mark.parametrize("provider_d", ["", "x" * 65, "node\nname"])
def test_provider_identifier_contract(provider_d: str) -> None:
    with pytest.raises(AnalyticsV2Error):
        build_analytics_address(
            Keys.parse(PRIVATE_KEY).public_key().to_hex(), provider_d, WEEK
        )


def test_shared_frontend_fixture_has_a_valid_signature_and_daily_partitions() -> None:
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "analytics-v2"
        / "daily-models-signed.json"
    )
    event = json.loads(fixture.read_text())
    frame = json.dumps(
        ["EVENT", event], separators=(",", ":"), ensure_ascii=False
    ).encode()
    parsed = prior_version_from_frame(frame)
    assert parsed.epoch == 3
    assert parsed.through == WEEK + timedelta(days=3)
    _assert_partitions(json.loads(event["content"]))


def test_size_folding_an_ordinary_extension_marks_changed_model_detail_as_correction() -> (
    None
):
    rows = [_outcome(model=f"model/{index}/" + "x" * 80) for index in range(30)]
    first = _encode(
        _aggregate(rows, today_utc=WEEK + timedelta(days=1)),
        max_frame_bytes=4_000,
    )
    extended_rows = rows + [
        replace(row, terminal_day=WEEK + timedelta(days=1)) for row in rows
    ]
    extended = _encode(
        _aggregate(
            extended_rows,
            today_utc=WEEK + timedelta(days=2),
            prior_version=first.as_prior_version(),
        ),
        max_frame_bytes=4_000,
        created_at=first.created_at + 1,
    )
    assert (
        _payload(first)["daily_models"][WEEK.isoformat()]
        != _payload(extended)["daily_models"][WEEK.isoformat()]
    )
    assert _payload(extended)["corrects"] == first.event_id
    _assert_partitions(_payload(extended))
