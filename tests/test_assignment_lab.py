from __future__ import annotations

from pathlib import Path
import json
import shutil
import tempfile
import unittest

from variantgrid.assignment import AssignmentEngine
from variantgrid.assignment_lab import (
    AssignmentEvidenceError,
    AssignmentLab,
    assignment_lab_snapshot,
    load_assignment_evidence,
)
from variantgrid.models import Experiment, VariantState
from evidence_fixtures import write_approved_evidence


def experiment(version: int) -> Experiment:
    return Experiment(
        key="assignment_lab_demo",
        version=version,
        states=(
            VariantState("control", {"experience": "control"}, 1.0),
            VariantState("guided", {"experience": "guided"}, 1.0),
        ),
        primary_metric="activated",
        salt="assignment-lab-test",
        assignment_algorithm_version="sha256-v1",
        policy_version="fixed-weight-v1",
    )


class AssignmentLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.current = experiment(1)
        self.comparison = experiment(2)
        self.directory = tempfile.TemporaryDirectory()
        self.project_root = write_approved_evidence(Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_snapshot_is_compact_json_serializable_and_repeatable(self) -> None:
        snapshot = assignment_lab_snapshot(
            "subject-42",
            self.current,
            self.comparison,
            self.project_root,
        )
        json.dumps(snapshot)
        self.assertEqual(snapshot["schema_version"], "1")
        self.assertEqual(snapshot["subject_id"], "subject-42")
        self.assertTrue(snapshot["same_version"]["identical"])
        self.assertEqual(
            snapshot["same_version"]["first"],
            snapshot["same_version"]["repeat"],
        )

    def test_assignment_record_exposes_auditable_bucket_and_policy_diagnostics(self) -> None:
        snapshot = AssignmentLab(self.current, self.comparison, self.project_root).snapshot("subject-1")
        assignment = snapshot["same_version"]["first"]
        self.assertEqual(assignment["version"], 1)
        self.assertEqual(assignment["algorithm_version"], "sha256-v1")
        self.assertEqual(assignment["policy_version"], "fixed-weight-v1")
        self.assertEqual(assignment["probability"], 0.5)
        self.assertLessEqual(assignment["interval"]["start"], assignment["bucket"])
        self.assertLess(assignment["bucket"], assignment["interval"]["end"])
        self.assertIsNotNone(assignment["state_id"])

    def test_version_isolation_does_not_require_the_subject_to_change_state(self) -> None:
        current = AssignmentEngine(self.current)
        comparison = AssignmentEngine(self.comparison)
        same_state_subject = next(
            subject
            for subject in (f"subject-{index}" for index in range(1_000))
            if current.assign(subject).state_id == comparison.assign(subject).state_id
        )
        proof = AssignmentLab(self.current, self.comparison, self.project_root).snapshot(
            same_state_subject
        )["version_isolation"]
        self.assertTrue(proof["isolated"])
        self.assertTrue(proof["namespace_changed"])
        self.assertFalse(proof["state_changed"])
        self.assertIn("same state is valid", proof["interpretation"])

    def test_evidence_is_loaded_from_the_approved_generated_artifacts(self) -> None:
        evidence = load_assignment_evidence(self.project_root)
        benchmark = json.loads((self.project_root / "benchmarks" / "results.json").read_text())
        claim = json.loads((self.project_root / "artifacts" / "claims" / "VG-2R.json").read_text())
        self.assertEqual(evidence["status"], "approved")
        self.assertEqual(evidence["wording"], claim["wording"])
        self.assertEqual(
            {row["policy"] for row in evidence["allocation_policies"]},
            {"50_50", "80_20", "90_10"},
        )
        self.assertTrue(all(row["assignments"] >= 1_000_000 for row in evidence["allocation_policies"]))
        self.assertTrue(all(row["stickiness_percent"] == 100.0 for row in evidence["allocation_policies"]))
        self.assertEqual(
            evidence["latency"]["trials"],
            [
                {"trial": index, **trial}
                for index, trial in enumerate(
                    benchmark["assignment_performance"]["trials"], start=1
                )
            ],
        )
        self.assertEqual(
            evidence["latency"]["median_sampled_p95_microseconds"],
            benchmark["assignment_performance"]["median_trial_sampled_p95_microseconds"],
        )

    def test_evidence_includes_provenance_checksums_environment_and_limitations(self) -> None:
        evidence = load_assignment_evidence(self.project_root)
        provenance = evidence["provenance"]
        self.assertEqual(provenance["methodology_version"], "3.0")
        self.assertEqual(len(provenance["source_fingerprint_sha256"]), 64)
        self.assertEqual(
            set(provenance["artifacts"]),
            {"claim", "benchmark", "manifest", "release_audit"},
        )
        self.assertTrue(all(len(value) == 64 for value in provenance["artifact_sha256"].values()))
        self.assertTrue(provenance["reproduction_command"])
        self.assertTrue(provenance["environment"])
        self.assertEqual(evidence["latency"]["scope"], "local_in_process")
        self.assertTrue(evidence["limitations"])

    def test_unapproved_or_incomplete_evidence_fails_visibly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "artifacts/claims/VG-2R.json",
                "artifacts/release_manifest.json",
                "artifacts/release_audit.json",
                "benchmarks/results.json",
            ):
                source = self.project_root / relative
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            claim_path = root / "artifacts" / "claims" / "VG-2R.json"
            claim = json.loads(claim_path.read_text())
            claim["status"] = "blocked"
            claim_path.write_text(json.dumps(claim))
            with self.assertRaisesRegex(AssignmentEvidenceError, "not an approved"):
                load_assignment_evidence(root)

    def test_lab_rejects_invalid_subjects_and_non_version_comparisons(self) -> None:
        with self.assertRaisesRegex(ValueError, "subject_id"):
            AssignmentLab(self.current, self.comparison, self.project_root).snapshot(" ")
        with self.assertRaisesRegex(ValueError, "same experiment key"):
            AssignmentLab(
                self.current,
                Experiment(
                    key="different",
                    version=2,
                    states=self.current.states,
                    primary_metric="activated",
                    salt="assignment-lab-test",
                ),
                self.project_root,
            )
        with self.assertRaisesRegex(ValueError, "different versions"):
            AssignmentLab(self.current, self.current, self.project_root)


if __name__ == "__main__":
    unittest.main()
