from __future__ import annotations

import json
import unittest

from variantgrid.integrity_lab import LAB_SCHEMA_VERSION, run_event_integrity_lab


class IntegrityLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = run_event_integrity_lab()
        self.rows = {row.scenario: row for row in self.report.rows}

    def test_report_is_ui_ready_serializable_and_scope_limited(self) -> None:
        payload = self.report.to_dict()
        self.assertEqual(payload["schema_version"], LAB_SCHEMA_VERSION)
        self.assertEqual(payload["storage_backend"], "sqlite-memory")
        self.assertFalse(payload["distributed_behavior_claimed"])
        self.assertIn("not distributed", str(payload["scope_note"]).lower())
        self.assertEqual(
            payload["summary"],
            {"total": 5, "passed": 5, "failed": 0, "all_passed": True},
        )
        self.assertEqual(json.loads(self.report.to_json()), payload)
        for row in payload["rows"]:
            self.assertEqual(
                {
                    "scenario",
                    "fixture",
                    "expected_invariant",
                    "observed",
                    "input_trace",
                    "reconciled_event_ids",
                    "passed",
                    "status",
                    "reproduction_command",
                },
                set(row),
            )
            self.assertTrue(row["expected_invariant"])
            self.assertTrue(row["observed"])
            self.assertTrue(row["passed"])
            self.assertEqual(row["status"], "pass")

    def test_scenario_order_and_json_are_deterministic(self) -> None:
        self.assertEqual(
            [row.scenario for row in self.report.rows],
            ["duplicate", "missing_exposure", "late", "reordered", "cross_version"],
        )
        self.assertEqual(self.report.to_json(), run_event_integrity_lab().to_json())

    def test_duplicate_row_uses_real_idempotent_ingestion(self) -> None:
        row = self.rows["duplicate"]
        self.assertTrue(row.passed)
        self.assertEqual(row.observed["accepted_deliveries"], 3)
        self.assertEqual(row.observed["duplicate_deliveries"], 2)
        self.assertEqual(
            row.observed["stored_event_type_counts"],
            {"assignment": 1, "exposure": 1, "goal": 1},
        )
        self.assertEqual(row.observed["binary_counts"], {"control": (1, 1)})
        self.assertEqual(
            [item["event_type"] for item in row.input_trace],
            ["assignment", "exposure", "exposure", "goal", "goal"],
        )

    def test_missing_exposure_row_excludes_assignment_only_subject(self) -> None:
        row = self.rows["missing_exposure"]
        self.assertTrue(row.passed)
        self.assertEqual(row.observed["assignment_count"], 1)
        self.assertEqual(row.observed["exposure_count"], 0)
        self.assertEqual(row.observed["goal_count"], 1)
        self.assertEqual(row.observed["binary_counts"], {})
        self.assertTrue(row.observed["outcome_excluded"])
        self.assertEqual(
            [item["event_type"] for item in row.input_trace], ["assignment", "goal"]
        )

    def test_late_row_reconciles_by_event_time_not_receipt_order(self) -> None:
        row = self.rows["late"]
        self.assertTrue(row.passed)
        self.assertEqual(
            row.observed["receipt_order_event_ids"], ("late-goal", "late-exposure")
        )
        self.assertEqual(
            row.observed["event_time_order_event_ids"], ("late-exposure", "late-goal")
        )
        self.assertEqual(row.observed["binary_counts"], {"control": (1, 1)})

    def test_reordered_row_matches_chronological_reconciliation_digest(self) -> None:
        row = self.rows["reordered"]
        self.assertTrue(row.passed)
        self.assertNotEqual(
            row.observed["chronological_receipt_order"],
            row.observed["reordered_receipt_order"],
        )
        self.assertEqual(
            row.observed["chronological_counts"], row.observed["reordered_counts"]
        )
        self.assertEqual(
            row.observed["chronological_digest"], row.observed["reordered_digest"]
        )
        self.assertTrue(row.observed["digests_match"])

    def test_cross_version_row_prevents_outcome_leakage(self) -> None:
        row = self.rows["cross_version"]
        self.assertTrue(row.passed)
        self.assertEqual(row.observed["version_1_binary_counts"], {"control": (1, 0)})
        self.assertEqual(row.observed["version_2_binary_counts"], {})
        self.assertEqual(row.observed["version_1_converter_count"], 0)
        self.assertEqual(row.observed["version_2_converter_count"], 0)
        self.assertFalse(row.observed["cross_version_conversion_leaked"])
        self.assertEqual(
            [item["experiment_version"] for item in row.input_trace], [1, 1, 2]
        )


if __name__ == "__main__":
    unittest.main()
