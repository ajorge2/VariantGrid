from __future__ import annotations

from hashlib import sha256
import json
import unittest

from variantgrid.event_storage import (
    BatchIdCollisionError,
    DeadLetterRecord,
    EventIdCollisionError,
    EventRecord,
    EventRepository,
)


TIMESTAMP = "2026-01-01T12:00:00+00:00"


def record(event_id: str, *, subject_id: str = "u-1", occurred_at: str = TIMESTAMP) -> EventRecord:
    payload = {
        "event_id": event_id,
        "experiment_key": "checkout",
        "experiment_version": 1,
        "subject_id": subject_id,
        "event_type": "exposure",
        "variant_key": "control",
        "metric": None,
        "value": None,
        "occurred_at": occurred_at,
        "schema_version": 1,
    }
    digest = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return EventRecord(
        event_id=event_id,
        experiment_key="checkout",
        experiment_version=1,
        subject_id=subject_id,
        event_type="exposure",
        variant_key="control",
        metric=None,
        value=None,
        occurred_at=occurred_at,
        received_at=TIMESTAMP,
        schema_version=1,
        payload_hash=digest,
    )


class EventRepositoryContractMixin:
    """Executable persistence contract shared by every repository adapter.

    A future Postgres integration test should subclass this mixin, implement
    ``make_repository``, and run the exact same semantics against an isolated
    database schema. Passing the SQLite subclass does not imply Postgres passes.
    """

    repository: EventRepository

    def make_repository(self) -> EventRepository:
        raise NotImplementedError

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.repository = self.make_repository()

    def tearDown(self) -> None:
        self.repository.close()
        super().tearDown()  # type: ignore[misc]

    def test_contract_identical_insert_is_idempotent_and_collision_is_visible(self) -> None:
        self.assertTrue(self.repository.insert_event(record("event-1")))
        self.assertFalse(self.repository.insert_event(record("event-1")))
        with self.assertRaises(EventIdCollisionError):
            self.repository.insert_event(record("event-1", subject_id="different"))
        self.assertEqual(self.repository.count_events(), 1)

    def test_contract_batch_result_is_recoverable_and_batch_identity_is_immutable(self) -> None:
        events = (record("event-1"), record("event-2"))
        first = self.repository.insert_batch("batch-1", "hash-1", events, (), TIMESTAMP)
        replay = self.repository.insert_batch("batch-1", "hash-1", events, (), TIMESTAMP)
        self.assertEqual((first.inserted, first.duplicates, first.dead_lettered), (2, 0, 0))
        self.assertTrue(replay.replayed)
        self.assertEqual(first.inserted, replay.inserted)
        self.assertEqual(self.repository.count_events(), 2)
        with self.assertRaises(BatchIdCollisionError):
            self.repository.insert_batch("batch-1", "other-hash", (), (), TIMESTAMP)

    def test_contract_batch_quarantine_and_dead_letter_lifecycle_are_durable(self) -> None:
        letter = DeadLetterRecord("{}", "invalid_schema", "missing subject", TIMESTAMP)
        result = self.repository.insert_batch("batch-1", "hash-1", (), (letter,), TIMESTAMP)
        self.assertEqual(result.dead_lettered, 1)
        pending = self.repository.list_dead_letters("pending")
        self.assertEqual(len(pending), 1)
        identifier = int(pending[0]["dead_letter_id"])
        self.repository.mark_dead_letter_attempt(identifier, "still invalid")
        self.assertEqual(int(self.repository.get_dead_letter(identifier)["attempt_count"]), 1)
        self.repository.mark_dead_letter_resolved(identifier, TIMESTAMP)
        resolved = self.repository.get_dead_letter(identifier)
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(int(resolved["attempt_count"]), 2)

    def test_contract_event_and_receipt_order_are_independently_queryable(self) -> None:
        late_event = record("event-late", occurred_at="2026-01-01T13:00:00+00:00")
        early_event = record("event-early", occurred_at="2026-01-01T11:00:00+00:00")
        self.repository.insert_event(late_event)
        self.repository.insert_event(early_event)
        event_order = [row["event_id"] for row in self.repository.fetch_events(order="event")]
        receipt_order = [row["event_id"] for row in self.repository.fetch_events(order="receipt")]
        self.assertEqual(event_order, ["event-early", "event-late"])
        self.assertEqual(receipt_order, ["event-late", "event-early"])


__all__ = ["EventRepositoryContractMixin"]
