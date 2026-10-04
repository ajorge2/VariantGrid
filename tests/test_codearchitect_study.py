from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


STUDY_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "studies"
    / "codearchitect-threshold-evaluation"
    / "run_evaluation.py"
)
SPEC = importlib.util.spec_from_file_location("codearchitect_study", STUDY_SCRIPT)
assert SPEC and SPEC.loader
study = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(study)


def write_fixture(root: Path, *, missing_label: bool = False) -> None:
    manifest = {
        "schema_version": "codearchitect-human-review/v1",
        "holdout_components": 10,
        "review_items": 3,
        "semantic_accepts": 3,
        "baseline_accepts": 1,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "preregistration.md").write_text("frozen before human review\n", encoding="utf-8")
    key_rows = (
        {
            "review_id": "one",
            "methods": ["semantic_ast", "exact_token_ast"],
            "semantic_similarity": 0.80,
        },
        {"review_id": "two", "methods": ["semantic_ast"], "semantic_similarity": 0.70},
        {"review_id": "three", "methods": ["semantic_ast"], "semantic_similarity": 0.65},
    )
    (root / "review-key.jsonl").write_text(
        "\n".join(json.dumps(row) for row in key_rows) + "\n", encoding="utf-8"
    )
    with (root / "human-review.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("review_id", "review_label"))
        writer.writeheader()
        writer.writerows(
            (
                {"review_id": "one", "review_label": "correct"},
                {"review_id": "two", "review_label": "" if missing_label else "incorrect"},
                {"review_id": "three", "review_label": "correct"},
            )
        )


class CodeArchitectStudyTests(unittest.TestCase):
    def test_variantgrid_replays_labels_and_persists_a_no_ship_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            write_fixture(evidence)
            registry = root / "variantgrid.sqlite"
            output = root / "decision-snapshot.json"

            snapshot = study.run_replay(
                evidence_dir=evidence,
                registry_path=registry,
                output_path=output,
            )

            result = snapshot["result"]
            self.assertEqual("no-ship", result["decision"])
            self.assertAlmostEqual(0.30, result["candidate_0_62"]["coverage"])
            self.assertAlmostEqual(2 / 3, result["candidate_0_62"]["strict_precision"])
            self.assertIn(
                "candidate_strict_precision_at_least_80_percent", result["failed_gates"]
            )
            self.assertTrue(registry.is_file())
            self.assertTrue(output.is_file())
            persisted = snapshot["variantgrid_registry"]["decisions"]
            self.assertEqual(1, len(persisted))
            self.assertEqual("no-ship", persisted[0]["decision"])
            self.assertEqual("stopped", snapshot["variantgrid_registry"]["lifecycle"])
            self.assertEqual(
                "paired offline model-policy evaluation",
                persisted[0]["evidence"]["study_type"],
            )

    def test_incomplete_human_review_is_rejected_before_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            write_fixture(evidence, missing_label=True)
            with self.assertRaisesRegex(ValueError, "incomplete or invalid"):
                study.run_replay(
                    evidence_dir=evidence,
                    registry_path=root / "variantgrid.sqlite",
                    output_path=root / "decision-snapshot.json",
                )
            self.assertFalse((root / "variantgrid.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
