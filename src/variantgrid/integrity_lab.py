from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Mapping, Sequence

from .events import Event, EventStore


LAB_SCHEMA_VERSION = "1"
EXPERIMENT_KEY = "integrity-lab-checkout"
METRIC = "purchased"
VARIANT = "control"
SUBJECT = "fixture-subject"
BASE_TIME = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
RECEIPT_TIME = BASE_TIME + timedelta(days=7)
LOCAL_SCOPE_NOTE = (
    "Deterministic in-memory SQLite evidence. This lab demonstrates local ingestion and "
    "reconciliation semantics, not distributed delivery, Postgres throughput, or availability."
)


@dataclass(frozen=True)
class IntegrityLabRow:
    scenario: str
    fixture: str
    expected_invariant: str
    observed: Mapping[str, object]
    input_trace: tuple[Mapping[str, object], ...]
    reconciled_event_ids: tuple[str, ...]
    passed: bool
    reproduction_command: str

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "fixture": self.fixture,
            "expected_invariant": self.expected_invariant,
            "observed": _json_value(self.observed),
            "input_trace": [_json_value(item) for item in self.input_trace],
            "reconciled_event_ids": list(self.reconciled_event_ids),
            "passed": self.passed,
            "status": "pass" if self.passed else "fail",
            "reproduction_command": self.reproduction_command,
        }


@dataclass(frozen=True)
class IntegrityLabReport:
    rows: tuple[IntegrityLabRow, ...]
    schema_version: str = LAB_SCHEMA_VERSION
    storage_backend: str = "sqlite-memory"
    scope_note: str = LOCAL_SCOPE_NOTE

    @property
    def all_passed(self) -> bool:
        return all(row.passed for row in self.rows)

    def to_dict(self) -> dict[str, object]:
        passed = sum(row.passed for row in self.rows)
        return {
            "schema_version": self.schema_version,
            "storage_backend": self.storage_backend,
            "scope_note": self.scope_note,
            "distributed_behavior_claimed": False,
            "summary": {
                "total": len(self.rows),
                "passed": passed,
                "failed": len(self.rows) - passed,
                "all_passed": passed == len(self.rows),
            },
            "rows": [row.to_dict() for row in self.rows],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def run_event_integrity_lab() -> IntegrityLabReport:
    """Execute the five protected event-integrity fixtures through EventStore."""
    return IntegrityLabReport(
        rows=(
            _duplicate_scenario(),
            _missing_exposure_scenario(),
            _late_delivery_scenario(),
            _reordered_scenario(),
            _cross_version_scenario(),
        )
    )


def _store(*, versions: set[int] | None = None) -> EventStore:
    validator = None
    if versions is not None:
        validator = lambda key, version: key == EXPERIMENT_KEY and version in versions
    return EventStore(clock=lambda: RECEIPT_TIME, version_validator=validator)


def _assignment(event_id: str, *, version: int = 1) -> Event:
    return Event(
        event_id,
        EXPERIMENT_KEY,
        version,
        SUBJECT,
        "assignment",
        VARIANT,
        occurred_at=_at(hours=0),
    )


def _exposure(event_id: str, *, version: int = 1, hours: float = 1) -> Event:
    return Event(
        event_id,
        EXPERIMENT_KEY,
        version,
        SUBJECT,
        "exposure",
        VARIANT,
        occurred_at=_at(hours=hours),
    )


def _goal(event_id: str, *, version: int = 1, hours: float = 2) -> Event:
    return Event(
        event_id,
        EXPERIMENT_KEY,
        version,
        SUBJECT,
        "goal",
        metric=METRIC,
        value=1,
        occurred_at=_at(hours=hours),
    )


def _duplicate_scenario() -> IntegrityLabRow:
    command = _command("test_duplicate_row_uses_real_idempotent_ingestion")
    deliveries = (
        _assignment("duplicate-assignment"),
        _exposure("duplicate-exposure"),
        _exposure("duplicate-exposure"),
        _goal("duplicate-goal"),
        _goal("duplicate-goal"),
    )
    with _store(versions={1}) as store:
        accepted = tuple(store.ingest(event) for event in deliveries)
        snapshot = store.reconcile(EXPERIMENT_KEY, 1, METRIC)
        event_type_counts = {
            event_type: store.event_count(event_type)
            for event_type in ("assignment", "exposure", "goal")
        }
        observed = {
            "accepted_deliveries": sum(accepted),
            "duplicate_deliveries": len(accepted) - sum(accepted),
            "delivery_acceptance": list(accepted),
            "stored_event_type_counts": event_type_counts,
            "binary_counts": snapshot.binary_counts,
            "converter_count": _converter_count(snapshot.binary_counts),
        }
        passed = (
            accepted == (True, True, False, True, False)
            and event_type_counts == {"assignment": 1, "exposure": 1, "goal": 1}
            and snapshot.binary_counts == {VARIANT: (1, 1)}
        )
        return _row(
            scenario="duplicate",
            fixture="same stable exposure and goal IDs delivered twice",
            invariant=(
                "Assignment, exposure, and outcome remain distinct; retrying an identical "
                "exposure or outcome cannot change the exposed or converted counts."
            ),
            observed=observed,
            deliveries=deliveries,
            reconciled=snapshot.ordered_event_ids,
            passed=passed,
            command=command,
        )


def _missing_exposure_scenario() -> IntegrityLabRow:
    command = _command("test_missing_exposure_row_excludes_assignment_only_subject")
    deliveries = (_assignment("missing-assignment"), _goal("missing-goal"))
    with _store(versions={1}) as store:
        accepted = tuple(store.ingest(event) for event in deliveries)
        snapshot = store.reconcile(EXPERIMENT_KEY, 1, METRIC)
        observed = {
            "accepted_deliveries": sum(accepted),
            "assignment_count": store.event_count("assignment"),
            "exposure_count": store.event_count("exposure"),
            "goal_count": store.event_count("goal"),
            "binary_counts": snapshot.binary_counts,
            "converter_count": _converter_count(snapshot.binary_counts),
            "outcome_excluded": snapshot.binary_counts == {},
        }
        passed = (
            accepted == (True, True)
            and store.event_count("assignment") == 1
            and store.event_count("exposure") == 0
            and store.event_count("goal") == 1
            and snapshot.binary_counts == {}
        )
        return _row(
            scenario="missing_exposure",
            fixture="assignment and goal exist, but no exposure was recorded",
            invariant=(
                "Assignment does not count as exposure; an outcome without qualifying exposure "
                "must be excluded from the default readout."
            ),
            observed=observed,
            deliveries=deliveries,
            reconciled=snapshot.ordered_event_ids,
            passed=passed,
            command=command,
        )


def _late_delivery_scenario() -> IntegrityLabRow:
    command = _command("test_late_row_reconciles_by_event_time_not_receipt_order")
    deliveries = (
        _goal("late-goal", hours=2),
        _exposure("late-exposure", hours=1),
    )
    with _store(versions={1}) as store:
        for event in deliveries:
            store.ingest(event)
        snapshot = store.reconcile(EXPERIMENT_KEY, 1, METRIC)
        observed = {
            "receipt_order_event_ids": snapshot.receipt_order_event_ids,
            "event_time_order_event_ids": snapshot.ordered_event_ids,
            "binary_counts": snapshot.binary_counts,
            "converter_count": _converter_count(snapshot.binary_counts),
            "late_delivery_reconciled": snapshot.binary_counts == {VARIANT: (1, 1)},
        }
        passed = (
            snapshot.receipt_order_event_ids == ("late-goal", "late-exposure")
            and snapshot.ordered_event_ids == ("late-exposure", "late-goal")
            and snapshot.binary_counts == {VARIANT: (1, 1)}
        )
        return _row(
            scenario="late",
            fixture="exposure arrives after its outcome but has an earlier causal event time",
            invariant=(
                "Late delivery must reconcile using occurred_at; receipt order cannot erase a "
                "valid same-version exposure-to-outcome sequence."
            ),
            observed=observed,
            deliveries=deliveries,
            reconciled=snapshot.ordered_event_ids,
            passed=passed,
            command=command,
        )


def _reordered_scenario() -> IntegrityLabRow:
    command = _command("test_reordered_row_matches_chronological_reconciliation_digest")
    chronological = (
        _assignment("reordered-assignment"),
        _exposure("reordered-exposure"),
        _goal("reordered-goal"),
    )
    reversed_delivery = tuple(reversed(chronological))
    with _store(versions={1}) as baseline, _store(versions={1}) as reordered:
        baseline.ingest_batch("chronological-batch", chronological)
        reordered.ingest_batch("reversed-batch", reversed_delivery)
        expected = baseline.reconcile(EXPERIMENT_KEY, 1, METRIC)
        observed_snapshot = reordered.reconcile(EXPERIMENT_KEY, 1, METRIC)
        observed = {
            "chronological_receipt_order": expected.receipt_order_event_ids,
            "reordered_receipt_order": observed_snapshot.receipt_order_event_ids,
            "chronological_counts": expected.binary_counts,
            "reordered_counts": observed_snapshot.binary_counts,
            "chronological_digest": expected.digest,
            "reordered_digest": observed_snapshot.digest,
            "digests_match": expected.digest == observed_snapshot.digest,
        }
        passed = (
            expected.receipt_order_event_ids != observed_snapshot.receipt_order_event_ids
            and expected.binary_counts == {VARIANT: (1, 1)}
            and observed_snapshot.binary_counts == expected.binary_counts
            and observed_snapshot.digest == expected.digest
        )
        return _row(
            scenario="reordered",
            fixture="the same assignment, exposure, and goal are delivered in reverse order",
            invariant=(
                "Reordering delivery must not change the canonical event history, attributed "
                "counts, or reconciliation digest."
            ),
            observed=observed,
            deliveries=reversed_delivery,
            reconciled=observed_snapshot.ordered_event_ids,
            passed=passed,
            command=command,
        )


def _cross_version_scenario() -> IntegrityLabRow:
    command = _command("test_cross_version_row_prevents_outcome_leakage")
    deliveries = (
        _assignment("version-assignment", version=1),
        _exposure("version-1-exposure", version=1),
        _goal("version-2-goal", version=2),
    )
    with _store(versions={1, 2}) as store:
        for event in deliveries:
            store.ingest(event)
        version_one = store.reconcile(EXPERIMENT_KEY, 1, METRIC)
        version_two = store.reconcile(EXPERIMENT_KEY, 2, METRIC)
        observed = {
            "version_1_binary_counts": version_one.binary_counts,
            "version_2_binary_counts": version_two.binary_counts,
            "version_1_converter_count": _converter_count(version_one.binary_counts),
            "version_2_converter_count": _converter_count(version_two.binary_counts),
            "cross_version_conversion_leaked": False,
        }
        leaked = (
            _converter_count(version_one.binary_counts) > 0
            or _converter_count(version_two.binary_counts) > 0
        )
        observed["cross_version_conversion_leaked"] = leaked
        passed = (
            version_one.binary_counts == {VARIANT: (1, 0)}
            and version_two.binary_counts == {}
            and not leaked
        )
        return _row(
            scenario="cross_version",
            fixture="version 1 exposure and version 2 goal share the same subject",
            invariant=(
                "An outcome may only join an exposure from the same experiment version; "
                "cross-version contamination must produce zero conversions."
            ),
            observed=observed,
            deliveries=deliveries,
            reconciled=version_one.ordered_event_ids + version_two.ordered_event_ids,
            passed=passed,
            command=command,
        )


def _row(
    *,
    scenario: str,
    fixture: str,
    invariant: str,
    observed: Mapping[str, object],
    deliveries: Sequence[Event],
    reconciled: Sequence[str],
    passed: bool,
    command: str,
) -> IntegrityLabRow:
    return IntegrityLabRow(
        scenario=scenario,
        fixture=fixture,
        expected_invariant=invariant,
        observed=dict(observed),
        input_trace=tuple(_trace_event(index, event) for index, event in enumerate(deliveries, 1)),
        reconciled_event_ids=tuple(reconciled),
        passed=passed,
        reproduction_command=command,
    )


def _trace_event(delivery_index: int, event: Event) -> dict[str, object]:
    return {
        "delivery_index": delivery_index,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "experiment_version": event.experiment_version,
        "subject_id": event.subject_id,
        "variant_key": event.variant_key,
        "metric": event.metric,
        "value": event.value,
        "occurred_at": event.occurred_at,
    }


def _converter_count(counts: Mapping[str, tuple[int, int]]) -> int:
    return sum(converted for _, converted in counts.values())


def _at(*, hours: float) -> str:
    return (BASE_TIME + timedelta(hours=hours)).isoformat()


def _command(test_name: str) -> str:
    return f"PYTHONPATH=src python3 -m unittest tests.test_integrity_lab.IntegrityLabTests.{test_name} -v"


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


__all__ = [
    "IntegrityLabReport",
    "IntegrityLabRow",
    "LAB_SCHEMA_VERSION",
    "run_event_integrity_lab",
]
