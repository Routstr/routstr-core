from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from nostr_sdk import (
    Event,
    EventBuilder,
    Keys,
    Kind,
    Tag,
    Timestamp,
)

ANALYTICS_KIND = 38422
ANALYTICS_SCHEMA = "routstr.analytics.v2"
DEFAULT_MAX_FRAME_BYTES = 96 * 1024
MAX_SAFE_INTEGER = (1 << 53) - 1

COLUMNS = (
    "completed_requests",
    "input_observed_requests",
    "output_observed_requests",
    "cache_read_observed_requests",
    "cache_creation_observed_requests",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "revenue_msats",
    "input_estimated_requests",
    "output_estimated_requests",
    "cache_read_estimated_requests",
    "cache_creation_estimated_requests",
    "input_missing_requests",
    "output_missing_requests",
    "cache_read_missing_requests",
    "cache_creation_missing_requests",
    "measured_token_requests",
    "measured_tokens",
)

MetricVector = tuple[int, ...]
ZERO_VECTOR: MetricVector = (0,) * len(COLUMNS)


class AnalyticsV2Error(ValueError):
    pass


class FrameTooLargeError(AnalyticsV2Error):
    def __init__(self, frame_size: int, max_frame_bytes: int) -> None:
        self.frame_size = frame_size
        self.max_frame_bytes = max_frame_bytes
        super().__init__(
            f"Analytics v2 frame is {frame_size} bytes, limit is {max_frame_bytes}"
        )


@dataclass(frozen=True)
class LedgerOutcome:
    terminal_day: date
    model_identifier: str | None
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    revenue_msats: int
    input_source: str
    output_source: str
    cache_read_source: str
    cache_creation_source: str
    completed_requests: int = 1


@dataclass(frozen=True)
class DayAggregate:
    day: date
    values: MetricVector


@dataclass(frozen=True)
class ModelAggregate:
    identifier: str | None
    values: MetricVector


@dataclass(frozen=True)
class DailyModelAggregate:
    day: date
    models: tuple[ModelAggregate, ...]


@dataclass(frozen=True)
class PriorVersion:
    event_id: str
    pubkey: str
    d_tag: str
    created_at: int
    week: date
    epoch: int
    coverage_start: date
    through: date
    complete: bool
    corrected: bool
    days: tuple[DayAggregate, ...]
    daily_models: tuple[DailyModelAggregate, ...]


@dataclass(frozen=True)
class WeeklyAggregate:
    week: date
    epoch: int
    coverage_start: date
    through: date
    complete: bool
    days: tuple[DayAggregate, ...]
    daily_models: tuple[DailyModelAggregate, ...]
    prior_version: PriorVersion | None = None
    correction: bool = False


@dataclass(frozen=True)
class AnalyticsAddress:
    coordinate: str
    provider_hash: str
    d_tag: str
    week: date
    tags: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class EncodedAnalyticsEvent:
    event_id: str
    pubkey: str
    created_at: int
    signature: str
    tags: tuple[tuple[str, str], ...]
    content: bytes
    frame: bytes
    d_tag: str
    week: date
    epoch: int
    coverage_start: date
    through: date
    complete: bool
    corrected: bool
    days: tuple[DayAggregate, ...]
    daily_models: tuple[DailyModelAggregate, ...]

    @property
    def event(self) -> dict[str, Any]:
        return _event_dict(
            self.event_id,
            self.pubkey,
            self.created_at,
            self.tags,
            self.content.decode("utf-8"),
            self.signature,
        )

    def as_prior_version(self) -> PriorVersion:
        return PriorVersion(
            event_id=self.event_id,
            pubkey=self.pubkey,
            d_tag=self.d_tag,
            created_at=self.created_at,
            week=self.week,
            epoch=self.epoch,
            coverage_start=self.coverage_start,
            through=self.through,
            complete=self.complete,
            corrected=self.corrected,
            days=self.days,
            daily_models=self.daily_models,
        )


def build_analytics_address(
    public_key_hex: str, provider_d: str, week: date, epoch: int = 0
) -> AnalyticsAddress:
    _validate_public_key(public_key_hex)
    _validate_provider_d(provider_d)
    _validate_week(week)
    _validate_epoch(epoch)
    coordinate = f"38421:{public_key_hex}:{provider_d}"
    provider_hash = hashlib.sha256(coordinate.encode("utf-8")).hexdigest()[:16]
    d_tag = (
        f"routstr.analytics.v2:{provider_hash}:week:{week.isoformat()}:epoch:{epoch}"
    )
    return AnalyticsAddress(
        coordinate=coordinate,
        provider_hash=provider_hash,
        d_tag=d_tag,
        week=week,
        tags=(
            ("d", d_tag),
            ("a", coordinate),
            ("w", week.isoformat()),
        ),
    )


def aggregate_ledger_week(
    outcomes: Iterable[LedgerOutcome],
    *,
    epoch: int,
    epoch_coverage_start: date,
    epoch_coverage_end: date | None,
    week: date,
    today_utc: date,
    prior_version: PriorVersion | None = None,
    correction: bool = False,
) -> WeeklyAggregate | None:
    """Aggregate the covered part of one week, capped at yesterday in UTC."""
    _validate_epoch(epoch)
    _validate_date(epoch_coverage_start, "epoch_coverage_start")
    if epoch_coverage_end is not None:
        _validate_date(epoch_coverage_end, "epoch_coverage_end")
    _validate_week(week)
    _validate_date(today_utc, "today_utc")
    if not isinstance(correction, bool):
        raise AnalyticsV2Error("correction must be a boolean")

    coverage_start = max(epoch_coverage_start, week)
    last_complete_day = today_utc - timedelta(days=1)
    week_end = week + timedelta(days=6)
    through = min(last_complete_day, week_end)
    if epoch_coverage_end is not None:
        through = min(through, epoch_coverage_end)
    if through < coverage_start:
        return None

    daily = {day: ZERO_VECTOR for day in _date_range(coverage_start, through)}
    models: dict[date, dict[str | None, MetricVector]] = {day: {} for day in daily}
    for outcome in outcomes:
        _validate_date(outcome.terminal_day, "terminal_day")
        if not (coverage_start <= outcome.terminal_day <= through):
            continue
        _validate_outcome(outcome)
        values = _outcome_vector(outcome)
        daily[outcome.terminal_day] = _add_vectors(daily[outcome.terminal_day], values)
        identifier = (
            outcome.model_identifier
            if _valid_model_identifier(outcome.model_identifier)
            else None
        )
        day_models = models[outcome.terminal_day]
        day_models[identifier] = _add_vectors(
            day_models.get(identifier, ZERO_VECTOR), values
        )

    model_rows = tuple(
        DailyModelAggregate(
            day,
            tuple(
                ModelAggregate(identifier, values)
                for identifier, values in sorted(
                    rows.items(), key=lambda item: (item[0] is None, item[0] or "")
                )
            ),
        )
        for day, rows in models.items()
    )
    return WeeklyAggregate(
        week=week,
        epoch=epoch,
        coverage_start=coverage_start,
        through=through,
        complete=through == min(week_end, epoch_coverage_end or week_end),
        days=tuple(DayAggregate(day, values) for day, values in daily.items()),
        daily_models=model_rows,
        prior_version=prior_version,
        correction=correction,
    )


def encode_week_event(
    aggregate: WeeklyAggregate,
    *,
    private_key_hex: str,
    provider_d: str,
    created_at: int,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> EncodedAnalyticsEvent:
    """Build, sign and size-fold one immutable analytics v2 EVENT frame."""
    _validate_aggregate(aggregate)
    _validate_nonnegative_int(created_at, "created_at", maximum=None)
    _validate_positive_int(max_frame_bytes, "max_frame_bytes")
    private_key = _private_key(private_key_hex)
    public_key = private_key.public_key().to_hex()
    address = build_analytics_address(
        public_key, provider_d, aggregate.week, aggregate.epoch
    )
    corrected, corrects = _validate_successor(
        aggregate, address, created_at, public_key
    )
    base = _base_payload(aggregate, corrected=corrected, corrects=corrects)
    maximum = max((len(row.models) for row in aggregate.daily_models), default=0)
    best: EncodedAnalyticsEvent | None = None
    lower, upper = 0, maximum
    while lower <= upper:
        kept_count = (lower + upper) // 2
        payload = dict(base)
        payload["daily_models"] = _wire_daily_models(aggregate, kept_count)
        encoded = _sign_payload(
            payload, aggregate, address, private_key, public_key, created_at, corrected
        )
        if len(encoded.frame) <= max_frame_bytes:
            best = encoded
            lower = kept_count + 1
        else:
            upper = kept_count - 1
    if best is None:
        raise FrameTooLargeError(len(encoded.frame), max_frame_bytes)
    prior = aggregate.prior_version
    if prior is not None and not aggregate.correction:
        previous = {row.day: row.models for row in prior.daily_models}
        current = {row.day: row.models for row in best.daily_models}
        if any(current.get(day) != models for day, models in previous.items()):
            return encode_week_event(
                replace(aggregate, correction=True),
                private_key_hex=private_key_hex,
                provider_d=provider_d,
                created_at=created_at,
                max_frame_bytes=max_frame_bytes,
            )
    return best


def prior_version_from_frame(frame: bytes) -> PriorVersion:
    """Verify an encoder-owned EVENT frame and recover its successor state."""
    if not isinstance(frame, bytes):
        raise AnalyticsV2Error("frame must be bytes")
    try:
        message = json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AnalyticsV2Error("Invalid analytics EVENT frame") from error
    if (
        not isinstance(message, list)
        or len(message) != 2
        or message[0] != "EVENT"
        or not isinstance(message[1], dict)
    ):
        raise AnalyticsV2Error("Invalid analytics EVENT frame")

    event = message[1]
    required_event_keys = {
        "id",
        "pubkey",
        "created_at",
        "kind",
        "tags",
        "content",
        "sig",
    }
    if set(event) != required_event_keys:
        raise AnalyticsV2Error("Invalid analytics event fields")
    event_id = event["id"]
    public_key = event["pubkey"]
    created_at = event["created_at"]
    signature = event["sig"]
    content_text = event["content"]
    _validate_hex(event_id, 64, "event id")
    _validate_public_key(public_key)
    _validate_nonnegative_int(created_at, "created_at", maximum=None)
    _validate_hex(signature, 128, "signature")
    if event["kind"] != ANALYTICS_KIND or isinstance(event["kind"], bool):
        raise AnalyticsV2Error("Invalid analytics event kind")
    if not isinstance(content_text, str):
        raise AnalyticsV2Error("Analytics event content must be a string")

    tags = _parse_wire_tags(event["tags"])
    canonical_event = _event_dict(
        event_id, public_key, created_at, tags, content_text, signature
    )
    if frame != _frame_bytes(canonical_event):
        raise AnalyticsV2Error("Analytics EVENT frame is not canonical")

    try:
        signature_valid = Event.from_json(json.dumps(event)).verify()
    except Exception as error:
        raise AnalyticsV2Error("Invalid analytics event signature") from error
    if not signature_valid:
        raise AnalyticsV2Error("Invalid analytics event signature")

    try:
        payload = json.loads(content_text)
    except json.JSONDecodeError as error:
        raise AnalyticsV2Error("Invalid analytics content JSON") from error
    if not isinstance(payload, dict):
        raise AnalyticsV2Error("Analytics content must be an object")
    if _canonical_json(payload) != content_text.encode("utf-8"):
        raise AnalyticsV2Error("Analytics content is not canonical")
    if payload.get("schema") != ANALYTICS_SCHEMA:
        raise AnalyticsV2Error("Invalid analytics schema")
    if payload.get("columns") != list(COLUMNS):
        raise AnalyticsV2Error("Invalid analytics columns")

    week = _parse_wire_date(payload.get("week"), "week")
    _validate_week(week)
    coverage_start = _parse_wire_date(payload.get("coverage_start"), "coverage_start")
    through = _parse_wire_date(payload.get("through"), "through")
    epoch_value = payload.get("epoch")
    if isinstance(epoch_value, bool) or not isinstance(epoch_value, int):
        raise AnalyticsV2Error("epoch must be a non-negative integer")
    epoch = epoch_value
    complete = payload.get("complete")
    _validate_epoch(epoch)
    if not isinstance(complete, bool):
        raise AnalyticsV2Error("complete must be a boolean")

    corrected_value = payload.get("corrected", False)
    if corrected_value is not False and corrected_value is not True:
        raise AnalyticsV2Error("corrected must be a boolean")
    if "corrected" in payload and corrected_value is not True:
        raise AnalyticsV2Error("corrected may only be present when true")
    corrected = corrected_value is True
    if "corrects" in payload:
        _validate_hex(payload["corrects"], 64, "corrects")
        if not corrected:
            raise AnalyticsV2Error("corrects requires corrected true")

    days = _parse_wire_days(payload.get("days"), coverage_start, through)
    daily_models = _parse_wire_daily_models(payload.get("daily_models"), days)
    aggregate = WeeklyAggregate(
        week=week,
        epoch=epoch,
        coverage_start=coverage_start,
        through=through,
        complete=complete,
        days=days,
        daily_models=daily_models,
    )
    _validate_aggregate(aggregate)

    if tags[2] != ("w", week.isoformat()):
        raise AnalyticsV2Error("Week tag does not match content")
    coordinate_parts = tags[1][1].split(":", 2)
    if len(coordinate_parts) != 3 or coordinate_parts[:2] != ["38421", public_key]:
        raise AnalyticsV2Error("Invalid analytics provider coordinate")
    address = build_analytics_address(public_key, coordinate_parts[2], week, epoch)
    if tags != address.tags:
        raise AnalyticsV2Error("Analytics tags do not match the signed coordinate")

    return PriorVersion(
        event_id=event_id,
        pubkey=public_key,
        d_tag=address.d_tag,
        created_at=created_at,
        week=week,
        epoch=epoch,
        coverage_start=coverage_start,
        through=through,
        complete=complete,
        corrected=corrected,
        days=days,
        daily_models=daily_models,
    )


def _base_payload(
    aggregate: WeeklyAggregate, *, corrected: bool, corrects: str | None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": ANALYTICS_SCHEMA,
        "week": aggregate.week.isoformat(),
        "epoch": aggregate.epoch,
        "coverage_start": aggregate.coverage_start.isoformat(),
        "through": aggregate.through.isoformat(),
        "complete": aggregate.complete,
        "columns": list(COLUMNS),
        "days": {row.day.isoformat(): list(row.values) for row in aggregate.days},
    }
    if corrects is not None:
        payload["corrects"] = corrects
    if corrected:
        payload["corrected"] = True
    return payload


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _event_dict(
    event_id: str,
    public_key: str,
    created_at: int,
    tags: tuple[tuple[str, str], ...],
    content: str,
    signature: str,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "pubkey": public_key,
        "created_at": created_at,
        "kind": ANALYTICS_KIND,
        "tags": [list(tag) for tag in tags],
        "content": content,
        "sig": signature,
    }


def _frame_bytes(event: dict[str, Any]) -> bytes:
    return json.dumps(
        ["EVENT", event], separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _parse_wire_tags(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) != 3:
        raise AnalyticsV2Error("Analytics event must carry exactly three tags")
    tags: list[tuple[str, str]] = []
    for tag in value:
        if (
            not isinstance(tag, list)
            or len(tag) != 2
            or not all(isinstance(item, str) for item in tag)
        ):
            raise AnalyticsV2Error("Invalid analytics event tag")
        tags.append((tag[0], tag[1]))
    if tuple(tag[0] for tag in tags) != ("d", "a", "w"):
        raise AnalyticsV2Error("Analytics tags must be d, a and w")
    return tuple(tags)


def _parse_wire_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise AnalyticsV2Error(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise AnalyticsV2Error(f"{name} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise AnalyticsV2Error(f"{name} must be an ISO date")
    return parsed


def _parse_wire_days(
    value: object, coverage_start: date, through: date
) -> tuple[DayAggregate, ...]:
    if not isinstance(value, dict):
        raise AnalyticsV2Error("days must be an object")
    expected_days = _date_range(coverage_start, through)
    expected_keys = {day.isoformat() for day in expected_days}
    if set(value) != expected_keys:
        raise AnalyticsV2Error("days do not match the covered range")
    rows: list[DayAggregate] = []
    for day in expected_days:
        raw_values = value[day.isoformat()]
        if not isinstance(raw_values, list):
            raise AnalyticsV2Error("Daily metrics must be an array")
        values = tuple(raw_values)
        _validate_vector(values)
        rows.append(DayAggregate(day, values))
    return tuple(rows)


def _parse_wire_daily_models(
    value: object, days: tuple[DayAggregate, ...]
) -> tuple[DailyModelAggregate, ...]:
    if not isinstance(value, dict) or set(value) != {
        row.day.isoformat() for row in days
    }:
        raise AnalyticsV2Error("daily_models must exactly cover the days")
    rows: list[DailyModelAggregate] = []
    for day in days:
        models = value[day.day.isoformat()]
        if not isinstance(models, dict) or "_other" not in models:
            raise AnalyticsV2Error("Daily model rows require _other")
        model_rows: list[ModelAggregate] = []
        for identifier, raw_values in sorted(models.items()):
            if (
                not isinstance(identifier, str)
                or not identifier
                or not isinstance(raw_values, list)
            ):
                raise AnalyticsV2Error("Invalid daily model row")
            values = tuple(raw_values)
            _validate_vector(values)
            model_rows.append(ModelAggregate(identifier, values))
        rows.append(DailyModelAggregate(day.day, tuple(model_rows)))
    return tuple(rows)


def _sign_payload(
    payload: dict[str, Any],
    aggregate: WeeklyAggregate,
    address: AnalyticsAddress,
    private_key: Keys,
    public_key: str,
    created_at: int,
    corrected: bool,
) -> EncodedAnalyticsEvent:
    content = _canonical_json(payload)
    content_text = content.decode("utf-8")
    signed = (
        EventBuilder(Kind(ANALYTICS_KIND), content_text)
        .tags([Tag.parse(list(tag)) for tag in address.tags])
        .custom_created_at(Timestamp.from_secs(created_at))
        .finalize(private_key)
    )
    event = json.loads(signed.as_json())
    event_id = event["id"]
    signature = event["sig"]
    return EncodedAnalyticsEvent(
        event_id=event_id,
        pubkey=public_key,
        created_at=created_at,
        signature=signature,
        tags=address.tags,
        content=content,
        frame=_frame_bytes(event),
        d_tag=address.d_tag,
        week=aggregate.week,
        epoch=aggregate.epoch,
        coverage_start=aggregate.coverage_start,
        through=aggregate.through,
        complete=aggregate.complete,
        corrected=corrected,
        days=aggregate.days,
        daily_models=_parse_wire_daily_models(payload["daily_models"], aggregate.days),
    )


def _validate_successor(
    aggregate: WeeklyAggregate,
    address: AnalyticsAddress,
    created_at: int,
    public_key: str,
) -> tuple[bool, str | None]:
    prior = aggregate.prior_version
    if prior is None:
        if aggregate.correction:
            raise AnalyticsV2Error(
                "A correction requires the immediately prior version"
            )
        return False, None

    _validate_prior(prior)
    if prior.pubkey != public_key or prior.d_tag != address.d_tag:
        raise AnalyticsV2Error("Prior version belongs to a different coordinate")
    if prior.week != aggregate.week:
        raise AnalyticsV2Error("Prior version belongs to a different week")
    if created_at <= prior.created_at:
        raise AnalyticsV2Error("created_at must strictly increase")
    if aggregate.epoch < prior.epoch:
        raise AnalyticsV2Error("epoch must not decrease")

    current_days = {row.day: row.values for row in aggregate.days}
    prior_days = {row.day: row.values for row in prior.days}
    if aggregate.epoch == prior.epoch:
        if aggregate.coverage_start != prior.coverage_start:
            raise AnalyticsV2Error("coverage_start changed within an epoch")
        if aggregate.through < prior.through:
            raise AnalyticsV2Error("through must not decrease")
        if prior.complete and not aggregate.complete:
            raise AnalyticsV2Error("complete must not revert")
        if aggregate.correction:
            return True, prior.event_id
        if prior.complete:
            raise AnalyticsV2Error("A closed week can only be corrected")
        if aggregate.through == prior.through:
            raise AnalyticsV2Error("An ordinary version must advance through")
        if any(
            current_days.get(day) != values for day, values in prior_days.items()
        ) or daily_models_changed(aggregate, prior):
            raise AnalyticsV2Error("An ordinary version changed a published row")
    else:
        if aggregate.correction:
            raise AnalyticsV2Error("A correction cannot change epoch")
        if current_days.keys() & prior_days.keys():
            raise AnalyticsV2Error("A new epoch must not copy prior rows")

    return prior.corrected, None


def _validate_aggregate(aggregate: WeeklyAggregate) -> None:
    _validate_period(
        aggregate.week,
        aggregate.epoch,
        aggregate.coverage_start,
        aggregate.through,
        aggregate.complete,
        aggregate.days,
    )
    if not isinstance(aggregate.correction, bool):
        raise AnalyticsV2Error("correction must be a boolean")
    if tuple(row.day for row in aggregate.daily_models) != tuple(
        row.day for row in aggregate.days
    ):
        raise AnalyticsV2Error("daily_models must exactly cover the days")
    for day, models in zip(aggregate.days, aggregate.daily_models):
        identifiers: set[str | None] = set()
        model_total = ZERO_VECTOR
        for model in models.models:
            if model.identifier in identifiers:
                raise AnalyticsV2Error("model identifiers must be unique within a day")
            identifiers.add(model.identifier)
            _validate_vector(model.values)
            model_total = _add_vectors(model_total, model.values)
        if model_total != day.values:
            raise AnalyticsV2Error("daily model rows must partition that day's totals")


def _validate_prior(prior: PriorVersion) -> None:
    _validate_hex(prior.event_id, 64, "prior event id")
    _validate_public_key(prior.pubkey)
    _validate_nonnegative_int(prior.created_at, "prior created_at", maximum=None)
    _validate_period(
        prior.week,
        prior.epoch,
        prior.coverage_start,
        prior.through,
        prior.complete,
        prior.days,
    )
    if not isinstance(prior.corrected, bool):
        raise AnalyticsV2Error("prior corrected must be a boolean")


def _validate_period(
    week: date,
    epoch: int,
    coverage_start: date,
    through: date,
    complete: bool,
    days: tuple[DayAggregate, ...],
) -> None:
    _validate_week(week)
    _validate_epoch(epoch)
    _validate_date(coverage_start, "coverage_start")
    _validate_date(through, "through")
    if coverage_start < week or coverage_start > through:
        raise AnalyticsV2Error("Invalid coverage range")
    if through > week + timedelta(days=6):
        raise AnalyticsV2Error("through follows the event week")
    if not isinstance(complete, bool):
        raise AnalyticsV2Error("complete must be a boolean")
    if through == week + timedelta(days=6) and not complete:
        raise AnalyticsV2Error("A week covering Sunday must be complete")
    if tuple(row.day for row in days) != _date_range(coverage_start, through):
        raise AnalyticsV2Error("days must exactly cover the coverage range")
    for row in days:
        _validate_vector(row.values)


def daily_models_changed(aggregate: WeeklyAggregate, prior: PriorVersion) -> bool:
    # Compare the same public detail level after size folding.
    prior_limit = max(
        (
            sum(row.identifier != "_other" for row in day.models)
            for day in prior.daily_models
        ),
        default=0,
    )
    current = _wire_daily_models(aggregate, prior_limit)
    return any(
        current.get(day.day.isoformat())
        != {row.identifier: list(row.values) for row in day.models}
        for day in prior.daily_models
    )


def _wire_daily_models(
    aggregate: WeeklyAggregate, keep_count: int
) -> dict[str, dict[str, list[int]]]:
    result: dict[str, dict[str, list[int]]] = {}
    for day, models in zip(aggregate.days, aggregate.daily_models):
        named = sorted(
            (row for row in models.models if _valid_model_identifier(row.identifier)),
            key=lambda row: (-row.values[0], row.identifier or ""),
        )[:keep_count]
        named_total = _sum_vectors(row.values for row in named)
        other = tuple(total - value for total, value in zip(day.values, named_total))
        result[day.day.isoformat()] = {
            row.identifier: list(row.values)
            for row in named
            if row.identifier is not None
        }
        result[day.day.isoformat()]["_other"] = list(other)
    return result


def _outcome_vector(outcome: LedgerOutcome) -> MetricVector:
    sources = (
        outcome.input_source,
        outcome.output_source,
        outcome.cache_read_source,
        outcome.cache_creation_source,
    )
    tokens = (
        outcome.input_tokens,
        outcome.output_tokens,
        outcome.cache_read_input_tokens,
        outcome.cache_creation_input_tokens,
    )
    measured = sources[:2] == ("reported", "reported") and all(
        source == "reported" or (source == "missing" and value == 0)
        for source, value in zip(sources[2:], tokens[2:])
    )
    return (
        outcome.completed_requests,
        *(outcome.completed_requests * int(source == "reported") for source in sources),
        *tokens,
        outcome.revenue_msats,
        *(
            outcome.completed_requests * int(source == "estimated")
            for source in sources
        ),
        *(outcome.completed_requests * int(source == "missing") for source in sources),
        outcome.completed_requests if measured else 0,
        sum(tokens) if measured else 0,
    )


def _validate_outcome(outcome: LedgerOutcome) -> None:
    _validate_date(outcome.terminal_day, "terminal_day")
    _validate_positive_int(outcome.completed_requests, "completed_requests")
    for name in ("input", "output", "cache_read", "cache_creation"):
        source = getattr(outcome, f"{name}_source")
        if source not in ("reported", "estimated", "missing"):
            raise AnalyticsV2Error("Invalid usage provenance")
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "revenue_msats",
    ):
        _validate_nonnegative_int(getattr(outcome, name), name)


def _validate_vector(values: MetricVector) -> None:
    if not isinstance(values, tuple) or len(values) != len(COLUMNS):
        raise AnalyticsV2Error(
            f"Metric vectors must contain exactly {len(COLUMNS)} integers"
        )
    for index, value in enumerate(values):
        _validate_nonnegative_int(value, COLUMNS[index])
    for index in range(4):
        if values[1 + index] + values[10 + index] + values[14 + index] != values[0]:
            raise AnalyticsV2Error("Usage provenance must partition completed requests")
    if values[18] > min(values[1], values[2]):
        raise AnalyticsV2Error("Measured requests require reported input and output")
    if values[19] > sum(values[5:9]) or (values[18] == 0 and values[19] != 0):
        raise AnalyticsV2Error("Measured tokens must belong to measured requests")


def _sum_vectors(vectors: Iterable[MetricVector]) -> MetricVector:
    total = ZERO_VECTOR
    for values in vectors:
        total = _add_vectors(total, values)
    return total


def _add_vectors(left: MetricVector, right: MetricVector) -> MetricVector:
    result = tuple(a + b for a, b in zip(left, right))
    if any(value > MAX_SAFE_INTEGER for value in result):
        raise AnalyticsV2Error("An aggregate exceeds the maximum safe JSON integer")
    return result


def _date_range(start: date, end: date) -> tuple[date, ...]:
    return tuple(
        start + timedelta(days=offset) for offset in range((end - start).days + 1)
    )


def _valid_model_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value != "_other"


def _private_key(private_key_hex: str) -> Keys:
    _validate_hex(private_key_hex, 64, "private key")
    try:
        return Keys.parse(private_key_hex)
    except Exception as error:
        raise AnalyticsV2Error("Invalid private key") from error


def _validate_public_key(value: str) -> None:
    _validate_hex(value, 64, "public key")


def _validate_provider_d(value: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise AnalyticsV2Error("provider_d must contain 1 to 64 characters")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise AnalyticsV2Error("provider_d must not contain control characters")


def _validate_week(value: date) -> None:
    _validate_date(value, "week")
    if value.weekday() != 0:
        raise AnalyticsV2Error("week must be a Monday")


def _validate_epoch(value: int) -> None:
    _validate_nonnegative_int(value, "epoch", maximum=None)


def _validate_date(value: object, name: str) -> None:
    if type(value) is not date:
        raise AnalyticsV2Error(f"{name} must be a date")


def _validate_positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AnalyticsV2Error(f"{name} must be a positive integer")


def _validate_nonnegative_int(
    value: object, name: str, *, maximum: int | None = MAX_SAFE_INTEGER
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnalyticsV2Error(f"{name} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise AnalyticsV2Error(f"{name} exceeds the maximum safe JSON integer")


def _validate_hex(value: object, length: int, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != length
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AnalyticsV2Error(f"{name} must be {length} lowercase hex characters")
