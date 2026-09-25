from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .event_storage import (
    BatchIdCollisionError,
    DeadLetterRecord,
    EventIdCollisionError,
    EventRecord,
    EventRepository,
    SQLiteEventRepository,
)


EVENT_TYPES = frozenset({"assignment", "exposure", "goal", "observation", "guardrail"})
IDENTITY_LIMIT = 512


class EventRejected(ValueError):
    """A visible ingestion rejection with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Event:
    """Versioned event envelope with type-specific validation.

    ``occurred_at`` is producer event time. If omitted, ingestion assigns the
    same UTC instant to occurrence and receipt. Durable records retain both.
    """

    event_id: str
    experiment_key: str
    experiment_version: int
    subject_id: str
    event_type: str
    variant_key: str | None = None
    metric: str | None = None
    value: float | None = None
    occurred_at: str | None = None
    schema_version: int = 1

    def validate(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("experiment_key", self.experiment_key),
            ("subject_id", self.subject_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise EventRejected("invalid_schema", f"{name} must be a non-empty string")
            if len(value) > IDENTITY_LIMIT:
                raise EventRejected("invalid_schema", f"{name} exceeds {IDENTITY_LIMIT} characters")
        if type(self.experiment_version) is not int or self.experiment_version < 1:
            raise EventRejected("invalid_schema", "experiment_version must be a positive integer")
        if self.schema_version != 1:
            raise EventRejected("unsupported_schema_version", "only event schema version 1 is supported")
        if self.event_type not in EVENT_TYPES:
            raise EventRejected("invalid_schema", f"unsupported event type: {self.event_type}")

        if self.event_type in {"assignment", "exposure"}:
            if not isinstance(self.variant_key, str) or not self.variant_key.strip():
                raise EventRejected("invalid_schema", f"{self.event_type} events require variant_key")
            if self.metric is not None or self.value is not None:
                raise EventRejected(
                    "invalid_schema", f"{self.event_type} events cannot contain metric or value"
                )
        else:
            if not isinstance(self.metric, str) or not self.metric.strip():
                raise EventRejected("invalid_schema", f"{self.event_type} events require metric")
            if self.variant_key is not None:
                raise EventRejected(
                    "invalid_schema", f"{self.event_type} events cannot declare a variant"
                )
            if self.event_type in {"observation", "guardrail"} and self.value is None:
                raise EventRejected(
                    "invalid_schema", f"{self.event_type} events require a numeric value"
                )
            if self.value is not None and (
                isinstance(self.value, bool)
                or not isinstance(self.value, (int, float))
                or not math.isfinite(float(self.value))
            ):
                raise EventRejected("invalid_schema", "event value must be a finite number")

        if self.occurred_at is not None:
            _parse_timestamp(self.occurred_at, "occurred_at")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "Event":
        allowed = {
            "event_id", "experiment_key", "experiment_version", "subject_id",
            "event_type", "variant_key", "metric", "value", "occurred_at",
            "schema_version",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise EventRejected("invalid_schema", f"unknown event fields: {sorted(unknown)}")
        try:
            return cls(**payload)  # type: ignore[arg-type]
        except TypeError as error:
            raise EventRejected("invalid_schema", str(error)) from error

    def payload(self, *, occurred_at: str | None = None) -> dict[str, object]:
        payload = asdict(self)
        if occurred_at is not None:
            payload["occurred_at"] = occurred_at
        return payload


@dataclass(frozen=True)
class BatchIngestionResult:
    batch_id: str
    inserted: int
    duplicates: int
    dead_lettered: int
    replayed: bool


@dataclass(frozen=True)
class DeadLetter:
    dead_letter_id: int
    payload: Mapping[str, object]
    error_code: str
    error_message: str
    received_at: str
    status: str
    attempt_count: int
    last_error: str | None
    resolved_at: str | None


@dataclass(frozen=True)
class ReconciliationSnapshot:
    experiment_key: str
    experiment_version: int
    metric: str
    ordered_event_ids: tuple[str, ...]
    receipt_order_event_ids: tuple[str, ...]
    binary_counts: Mapping[str, tuple[int, int]]
    digest: str


VersionValidator = Callable[[str, int], bool]
EventAuthorizer = Callable[[str | None, Event], bool]


class EventStore:
    """Validated, idempotent ingestion and deterministic attribution facade.

    It depends only on ``EventRepository``. The bundled SQLite adapter keeps
    tests self-contained and leaves a narrow seam for a Postgres implementation.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        repository: EventRepository | None = None,
        version_validator: VersionValidator | None = None,
        authorizer: EventAuthorizer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if repository is not None and str(path) != ":memory:":
            raise ValueError("pass either path or repository, not both")
        self.repository = repository or SQLiteEventRepository(path)
        self.version_validator = version_validator
        self.authorizer = authorizer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Compatibility for local diagnostics; callers should use the repository seam.
        self.connection = getattr(self.repository, "connection", None)

    def _validate_boundary(self, event: Event, credential: str | None) -> None:
        event.validate()
        if self.version_validator is not None and not self.version_validator(
            event.experiment_key, event.experiment_version
        ):
            raise EventRejected(
                "unknown_experiment_version",
                f"unknown experiment version {event.experiment_key}:{event.experiment_version}",
            )
        if self.authorizer is not None and not self.authorizer(credential, event):
            raise EventRejected("unauthorized", "credential cannot ingest this experiment version")

    def _record_for(self, event: Event, received_at: str) -> EventRecord:
        occurred_at = event.occurred_at or received_at
        normalized = _normalize_timestamp(occurred_at, "occurred_at")
        # An SDK may omit occurred_at and retry after an acknowledgement loss.
        # Preserve the first receipt as stored event time, but hash the producer
        # payload (where occurred_at remains null) so the retry is a duplicate.
        hash_payload = (
            event.payload(occurred_at=normalized)
            if event.occurred_at is not None
            else event.payload()
        )
        payload_json = _canonical_json(hash_payload)
        return EventRecord(
            event_id=event.event_id,
            experiment_key=event.experiment_key,
            experiment_version=event.experiment_version,
            subject_id=event.subject_id,
            event_type=event.event_type,
            variant_key=event.variant_key,
            metric=event.metric,
            value=None if event.value is None else float(event.value),
            occurred_at=normalized,
            received_at=received_at,
            schema_version=event.schema_version,
            payload_hash=sha256(payload_json.encode("utf-8")).hexdigest(),
        )

    def ingest(self, event: Event, *, credential: str | None = None) -> bool:
        received_at = _utc_iso(self.clock())
        try:
            self._validate_boundary(event, credential)
            return self.repository.insert_event(self._record_for(event, received_at))
        except EventIdCollisionError as error:
            self._dead_letter(event.payload(), "event_id_collision", str(error), received_at)
            raise EventRejected("event_id_collision", str(error)) from error
        except EventRejected as error:
            self._dead_letter(event.payload(), error.code, str(error), received_at)
            raise

    def ingest_payload(self, payload: Mapping[str, object], *, credential: str | None = None) -> bool:
        received_at = _utc_iso(self.clock())
        try:
            event = Event.from_mapping(payload)
        except EventRejected as error:
            self._dead_letter(payload, error.code, str(error), received_at)
            raise
        return self.ingest(event, credential=credential)

    def ingest_many(self, events: Iterable[Event]) -> tuple[int, int]:
        """Backward-compatible ingestion; use ``ingest_batch`` for retry tokens."""
        inserted = 0
        duplicates = 0
        for event in events:
            if self.ingest(event):
                inserted += 1
            else:
                duplicates += 1
        return inserted, duplicates

    def ingest_batch(
        self,
        batch_id: str,
        events: Iterable[Event | Mapping[str, object]],
        *,
        credential: str | None = None,
    ) -> BatchIngestionResult:
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise EventRejected("invalid_batch_id", "batch_id must be a non-empty string")
        received_at = _utc_iso(self.clock())
        items = tuple(events)
        raw_payloads = [_raw_payload(item) for item in items]
        batch_hash = sha256(_canonical_json(raw_payloads).encode("utf-8")).hexdigest()
        records: list[EventRecord] = []
        dead_letters: list[DeadLetterRecord] = []
        for item, raw_payload in zip(items, raw_payloads, strict=True):
            try:
                event = item if isinstance(item, Event) else Event.from_mapping(item)
                self._validate_boundary(event, credential)
                records.append(self._record_for(event, received_at))
            except EventRejected as error:
                dead_letters.append(
                    DeadLetterRecord(
                        payload_json=_canonical_json(raw_payload),
                        error_code=error.code,
                        error_message=str(error),
                        received_at=received_at,
                    )
                )
        result = self.repository.insert_batch(
            batch_id, batch_hash, records, dead_letters, received_at
        )
        return BatchIngestionResult(
            batch_id=batch_id,
            inserted=result.inserted,
            duplicates=result.duplicates,
            dead_lettered=result.dead_lettered,
            replayed=result.replayed,
        )

    def _dead_letter(
        self,
        payload: Mapping[str, object],
        error_code: str,
        error_message: str,
        received_at: str,
    ) -> int:
        return self.repository.add_dead_letter(
            DeadLetterRecord(
                payload_json=_canonical_json(payload),
                error_code=error_code,
                error_message=error_message,
                received_at=received_at,
            )
        )

    def dead_letters(self, status: str = "pending") -> tuple[DeadLetter, ...]:
        return tuple(_dead_letter_from_row(row) for row in self.repository.list_dead_letters(status))

    def replay_dead_letter(self, dead_letter_id: int, *, credential: str | None = None) -> bool:
        row = self.repository.get_dead_letter(dead_letter_id)
        if row["status"] == "resolved":
            return False
        try:
            event = Event.from_mapping(json.loads(row["payload_json"]))
            self._validate_boundary(event, credential)
            received_at = _utc_iso(self.clock())
            self.repository.insert_event(self._record_for(event, received_at))
        except (EventRejected, EventIdCollisionError) as error:
            self.repository.mark_dead_letter_attempt(dead_letter_id, str(error))
            return False
        self.repository.mark_dead_letter_resolved(dead_letter_id, _utc_iso(self.clock()))
        return True

    def binary_counts(
        self,
        experiment_key: str,
        version: int,
        metric: str,
        *,
        attribution_window_hours: float = 168,
    ) -> dict[str, tuple[int, int]]:
        """Return exposed subjects and unique converters by first exposure.

        The attribution interval is closed at both ends. Receipt order never
        participates, and outcomes without same-version exposure are excluded.
        """
        if attribution_window_hours <= 0:
            raise ValueError("attribution_window_hours must be positive")
        events = self._events(experiment_key, version)
        exposures: dict[str, EventRecord] = {}
        for event in events:
            if event.event_type == "exposure" and event.variant_key is not None:
                exposures.setdefault(event.subject_id, event)

        converted: set[str] = set()
        window = timedelta(hours=attribution_window_hours)
        for event in events:
            if event.event_type != "goal" or event.metric != metric:
                continue
            if (event.value if event.value is not None else 1.0) <= 0:
                continue
            exposure = exposures.get(event.subject_id)
            if exposure is None:
                continue
            elapsed = _parse_timestamp(event.occurred_at) - _parse_timestamp(exposure.occurred_at)
            if timedelta(0) <= elapsed <= window:
                converted.add(event.subject_id)

        counts: dict[str, list[int]] = {}
        for subject_id, exposure in exposures.items():
            values = counts.setdefault(exposure.variant_key or "", [0, 0])
            values[0] += 1
            if subject_id in converted:
                values[1] += 1
        return {variant: (values[0], values[1]) for variant, values in sorted(counts.items())}

    def attributed_values(
        self,
        experiment_key: str,
        version: int,
        metric: str,
        *,
        event_type: str = "observation",
        attribution_window_hours: float = 168,
    ) -> dict[str, tuple[float, ...]]:
        """Return exposure-gated numeric observations grouped by first variant."""
        if event_type not in {"observation", "guardrail"}:
            raise ValueError("event_type must be observation or guardrail")
        if attribution_window_hours <= 0:
            raise ValueError("attribution_window_hours must be positive")
        events = self._events(experiment_key, version)
        exposures: dict[str, EventRecord] = {}
        for event in events:
            if event.event_type == "exposure" and event.variant_key is not None:
                exposures.setdefault(event.subject_id, event)
        values: dict[str, list[float]] = {}
        window = timedelta(hours=attribution_window_hours)
        for event in events:
            if event.event_type != event_type or event.metric != metric or event.value is None:
                continue
            exposure = exposures.get(event.subject_id)
            if exposure is None:
                continue
            elapsed = _parse_timestamp(event.occurred_at) - _parse_timestamp(exposure.occurred_at)
            if timedelta(0) <= elapsed <= window:
                values.setdefault(exposure.variant_key or "", []).append(event.value)
        return {variant: tuple(measurements) for variant, measurements in sorted(values.items())}

    def reconcile(
        self,
        experiment_key: str,
        version: int,
        metric: str,
        *,
        attribution_window_hours: float = 168,
    ) -> ReconciliationSnapshot:
        ordered = self._events(experiment_key, version)
        receipt_rows = self.repository.fetch_events(experiment_key, version, order="receipt")
        counts = self.binary_counts(
            experiment_key, version, metric,
            attribution_window_hours=attribution_window_hours,
        )
        digest_payload = {
            "experiment_key": experiment_key,
            "experiment_version": version,
            "metric": metric,
            "ordered_event_ids": [event.event_id for event in ordered],
            "binary_counts": counts,
            "attribution_window_hours": attribution_window_hours,
        }
        return ReconciliationSnapshot(
            experiment_key=experiment_key,
            experiment_version=version,
            metric=metric,
            ordered_event_ids=tuple(event.event_id for event in ordered),
            receipt_order_event_ids=tuple(row["event_id"] for row in receipt_rows),
            binary_counts=counts,
            digest=sha256(_canonical_json(digest_payload).encode("utf-8")).hexdigest(),
        )

    def export_events(
        self, experiment_key: str | None = None, version: int | None = None
    ) -> tuple[dict[str, object], ...]:
        """Return a stable, warehouse-compatible row boundary."""
        return tuple(dict(row) for row in self.repository.fetch_events(experiment_key, version))

    def event_count(self, event_type: str | None = None) -> int:
        if event_type is not None and event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event_type}")
        return self.repository.count_events(event_type)

    def _events(self, experiment_key: str, version: int) -> tuple[EventRecord, ...]:
        return tuple(_record_from_row(row) for row in self.repository.fetch_events(experiment_key, version))

    def close(self) -> None:
        self.repository.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _record_from_row(row: Mapping[str, object]) -> EventRecord:
    return EventRecord(
        event_id=str(row["event_id"]),
        experiment_key=str(row["experiment_key"]),
        experiment_version=int(row["experiment_version"]),
        subject_id=str(row["subject_id"]),
        event_type=str(row["event_type"]),
        variant_key=None if row["variant_key"] is None else str(row["variant_key"]),
        metric=None if row["metric"] is None else str(row["metric"]),
        value=None if row["value"] is None else float(row["value"]),
        occurred_at=str(row["occurred_at"]),
        received_at=str(row["received_at"]),
        schema_version=int(row["schema_version"]),
        payload_hash=str(row["payload_hash"]),
    )


def _dead_letter_from_row(row: Mapping[str, object]) -> DeadLetter:
    return DeadLetter(
        dead_letter_id=int(row["dead_letter_id"]),
        payload=json.loads(str(row["payload_json"])),
        error_code=str(row["error_code"]),
        error_message=str(row["error_message"]),
        received_at=str(row["received_at"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        last_error=None if row["last_error"] is None else str(row["last_error"]),
        resolved_at=None if row["resolved_at"] is None else str(row["resolved_at"]),
    )


def _parse_timestamp(value: str, field: str = "timestamp") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise EventRejected("invalid_schema", f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventRejected("invalid_schema", f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _normalize_timestamp(value: str, field: str = "timestamp") -> str:
    return _parse_timestamp(value, field).isoformat()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _raw_payload(item: Event | Mapping[str, object]) -> dict[str, object]:
    return item.payload() if isinstance(item, Event) else dict(item)


__all__ = [
    "BatchIdCollisionError", "BatchIngestionResult", "DeadLetter", "Event",
    "EventIdCollisionError", "EventRejected", "EventStore", "ReconciliationSnapshot",
]
