from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from variantgrid.event_storage import BatchIdCollisionError
from variantgrid.events import Event, EventRejected, EventStore


BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def at(*, hours: float = 0) -> str:
    return (BASE + timedelta(hours=hours)).isoformat()


def exposure(event_id: str, subject: str, *, version: int = 1, hours: float = 0) -> Event:
    return Event(
        event_id,
        "checkout",
        version,
        subject,
        "exposure",
        "control",
        occurred_at=at(hours=hours),
    )


def goal(event_id: str, subject: str, *, version: int = 1, hours: float = 1) -> Event:
    return Event(
        event_id,
        "checkout",
        version,
        subject,
        "goal",
        metric="purchased",
        value=1,
        occurred_at=at(hours=hours),
    )


class EventIngestionTests(unittest.TestCase):
    def test_all_event_types_have_strict_type_specific_schemas(self) -> None:
        valid = (
            Event("a", "checkout", 1, "u", "assignment", "control", occurred_at=at()),
            Event("e", "checkout", 1, "u", "exposure", "control", occurred_at=at()),
            Event("g", "checkout", 1, "u", "goal", metric="purchased", occurred_at=at()),
            Event("o", "checkout", 1, "u", "observation", metric="revenue", value=12.5, occurred_at=at()),
            Event("r", "checkout", 1, "u", "guardrail", metric="errors", value=0, occurred_at=at()),
        )
        for event in valid:
            event.validate()

        with self.assertRaisesRegex(EventRejected, "require variant_key"):
            Event("bad", "checkout", 1, "u", "exposure", occurred_at=at()).validate()
        with self.assertRaisesRegex(EventRejected, "cannot contain metric"):
            Event(
                "bad", "checkout", 1, "u", "assignment", "control",
                metric="purchased", occurred_at=at(),
            ).validate()
        with self.assertRaisesRegex(EventRejected, "require a numeric value"):
            Event(
                "bad", "checkout", 1, "u", "observation",
                metric="revenue", occurred_at=at(),
            ).validate()
        with self.assertRaisesRegex(EventRejected, "timezone"):
            exposure("bad", "u").__class__(
                "bad", "checkout", 1, "u", "exposure", "control",
                occurred_at="2026-01-01T12:00:00",
            ).validate()

    def test_exact_and_concurrent_duplicates_never_double_count(self) -> None:
        store = EventStore()
        self.addCleanup(store.close)
        event = exposure("stable", "u")
        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(store.ingest, [event] * 64))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(store.event_count(), 1)
        self.assertFalse(store.ingest(event))

    def test_event_id_reuse_for_different_payload_is_quarantined(self) -> None:
        store = EventStore()
        self.addCleanup(store.close)
        self.assertTrue(store.ingest(exposure("same", "u-1")))
        with self.assertRaisesRegex(EventRejected, "different payload"):
            store.ingest(exposure("same", "u-2"))
        letters = store.dead_letters()
        self.assertEqual(len(letters), 1)
        self.assertEqual(letters[0].error_code, "event_id_collision")
        self.assertEqual(store.event_count(), 1)

    def test_event_and_receipt_timestamps_are_both_retained(self) -> None:
        received = BASE + timedelta(days=2)
        store = EventStore(clock=lambda: received)
        self.addCleanup(store.close)
        store.ingest(exposure("e", "u", hours=3))
        row = store.export_events()[0]
        self.assertEqual(row["occurred_at"], at(hours=3))
        self.assertEqual(row["received_at"], received.isoformat())

    def test_goal_received_before_exposure_reconciles_by_event_time(self) -> None:
        store = EventStore()
        self.addCleanup(store.close)
        store.ingest(goal("g", "u", hours=1))
        store.ingest(exposure("e", "u", hours=0))
        snapshot = store.reconcile("checkout", 1, "purchased")
        self.assertEqual(snapshot.receipt_order_event_ids, ("g", "e"))
        self.assertEqual(snapshot.ordered_event_ids, ("e", "g"))
        self.assertEqual(snapshot.binary_counts, {"control": (1, 1)})

    def test_missing_exposure_and_pre_exposure_goal_are_excluded(self) -> None:
        store = EventStore()
        self.addCleanup(store.close)
        store.ingest(goal("missing", "without-exposure"))
        store.ingest(goal("early", "u", hours=-1))
        store.ingest(exposure("e", "u", hours=0))
        self.assertEqual(store.binary_counts("checkout", 1, "purchased"), {"control": (1, 0)})

    def test_late_events_use_event_time_and_window_endpoint_is_inclusive(self) -> None:
        store = EventStore()
        self.addCleanup(store.close)
        store.ingest(goal("at-end", "u-1", hours=24))
        store.ingest(goal("too-late", "u-2", hours=24.0001))
        store.ingest(exposure("e-1", "u-1"))
        store.ingest(exposure("e-2", "u-2"))
        self.assertEqual(
            store.binary_counts("checkout", 1, "purchased", attribution_window_hours=24),
            {"control": (2, 1)},
        )

    def test_reordered_batches_reconcile_to_the_same_digest(self) -> None:
        chronological = (exposure("e", "u"), goal("g", "u"))
        first = EventStore()
        second = EventStore()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.ingest_batch("first", chronological)
        second.ingest_batch("second", reversed(chronological))
        one = first.reconcile("checkout", 1, "purchased")
        two = second.reconcile("checkout", 1, "purchased")
        self.assertEqual(one.digest, two.digest)
        self.assertNotEqual(one.receipt_order_event_ids, two.receipt_order_event_ids)

    def test_retry_after_timeout_recovers_the_committed_batch_result(self) -> None:
        store = EventStore()
        self.addCleanup(store.close)
        events = (exposure("e", "u"), goal("g", "u"))
        committed = store.ingest_batch("retry-token", events)
        recovered = store.ingest_batch("retry-token", events)
        self.assertEqual((committed.inserted, committed.duplicates), (2, 0))
        self.assertEqual((recovered.inserted, recovered.duplicates), (2, 0))
        self.assertTrue(recovered.replayed)
        self.assertEqual(store.event_count(), 2)

        with self.assertRaises(BatchIdCollisionError):
            store.ingest_batch("retry-token", (exposure("different", "u"),))

    def test_versions_are_validated_and_never_cross_attribution(self) -> None:
        allowed = {("checkout", 1)}
        store = EventStore(version_validator=lambda key, version: (key, version) in allowed)
        self.addCleanup(store.close)
        store.ingest(exposure("v1-e", "u", version=1))
        with self.assertRaisesRegex(EventRejected, "unknown experiment version"):
            store.ingest(goal("v2-g", "u", version=2))
        self.assertEqual(store.binary_counts("checkout", 1, "purchased"), {"control": (1, 0)})
        self.assertEqual(store.event_count(), 1)

    def test_dead_letter_can_be_replayed_after_version_catalog_changes(self) -> None:
        allowed: set[tuple[str, int]] = set()
        store = EventStore(version_validator=lambda key, version: (key, version) in allowed)
        self.addCleanup(store.close)
        with self.assertRaises(EventRejected):
            store.ingest(exposure("e", "u"))
        letter = store.dead_letters()[0]
        self.assertFalse(store.replay_dead_letter(letter.dead_letter_id))
        allowed.add(("checkout", 1))
        self.assertTrue(store.replay_dead_letter(letter.dead_letter_id))
        self.assertEqual(store.event_count(), 1)
        resolved = store.dead_letters("resolved")[0]
        self.assertEqual(resolved.attempt_count, 2)

    def test_malformed_and_unauthorized_payloads_fail_visibly(self) -> None:
        store = EventStore(authorizer=lambda credential, event: credential == "secret")
        self.addCleanup(store.close)
        with self.assertRaisesRegex(EventRejected, "subject_id"):
            store.ingest_payload(
                {
                    "event_id": "malformed",
                    "experiment_key": "checkout",
                    "experiment_version": 1,
                    "event_type": "exposure",
                    "variant_key": "control",
                }
            )
        with self.assertRaisesRegex(EventRejected, "credential"):
            store.ingest(exposure("unauthorized", "u"), credential="wrong")
        self.assertEqual(
            [letter.error_code for letter in store.dead_letters()],
            ["invalid_schema", "unauthorized"],
        )
        self.assertEqual(store.event_count(), 0)

    def test_invalid_batch_members_are_quarantined_without_losing_valid_members(self) -> None:
        store = EventStore()
        self.addCleanup(store.close)
        malformed = {
            "event_id": "bad",
            "experiment_key": "checkout",
            "experiment_version": 1,
            "subject_id": "u",
            "event_type": "observation",
            "metric": "revenue",
        }
        result = store.ingest_batch("mixed", (exposure("good", "u"), malformed))
        self.assertEqual((result.inserted, result.dead_lettered), (1, 1))
        self.assertEqual(store.event_count(), 1)
        self.assertEqual(store.dead_letters()[0].error_code, "invalid_schema")

    def test_observations_and_guardrails_are_exposure_gated(self) -> None:
        store = EventStore()
        self.addCleanup(store.close)
        store.ingest(exposure("e", "u"))
        store.ingest(
            Event("o", "checkout", 1, "u", "observation", metric="revenue", value=14, occurred_at=at(hours=1))
        )
        store.ingest(
            Event("r", "checkout", 1, "u", "guardrail", metric="errors", value=2, occurred_at=at(hours=2))
        )
        store.ingest(
            Event("orphan", "checkout", 1, "x", "observation", metric="revenue", value=99, occurred_at=at(hours=1))
        )
        self.assertEqual(store.attributed_values("checkout", 1, "revenue"), {"control": (14.0,)})
        self.assertEqual(
            store.attributed_values("checkout", 1, "errors", event_type="guardrail"),
            {"control": (2.0,)},
        )

    def test_file_store_reopens_with_identical_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/events.sqlite3"
            first = EventStore(path)
            first.ingest_batch("persisted", (goal("g", "u"), exposure("e", "u")))
            before = first.reconcile("checkout", 1, "purchased")
            first.close()
            second = EventStore(path)
            self.addCleanup(second.close)
            after = second.reconcile("checkout", 1, "purchased")
            self.assertEqual(before.digest, after.digest)
            self.assertEqual(before.binary_counts, after.binary_counts)


if __name__ == "__main__":
    unittest.main()
