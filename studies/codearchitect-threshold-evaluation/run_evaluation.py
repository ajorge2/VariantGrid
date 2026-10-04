from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any

from variantgrid.models import Experiment, Factor, Guardrail, MetricDefinition, VariableType
from variantgrid.policy import PreRegisteredGate, evaluate_pre_registered_gates
from variantgrid.registry import ExperimentRegistry, Lifecycle


EXPERIMENT_KEY = "codearchitect_threshold_evaluation"
VERSION = 1
CHANGE = "lower semantic acceptance threshold from 0.72 to 0.62"
CANDIDATE_THRESHOLD = 0.62
CONSERVATIVE_THRESHOLD = 0.72
MINIMUM_COVERAGE = 0.20
MINIMUM_STRICT_PRECISION = 0.80
MAXIMUM_PRECISION_DROP = 0.05
VALID_LABELS = {"correct", "partial", "incorrect", "cannot_judge"}

STUDY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = STUDY_DIR.parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_EVIDENCE_DIR = WORKSPACE_ROOT / "codearchitect-decision-evidence"


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    radius = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / total + z * z / (4 * total * total)
        )
        / denominator
    )
    return (center - radius, center + radius)


def _load_reviews(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    reviews: dict[str, str] = {}
    for row in rows:
        review_id = row.get("review_id", "").strip()
        label = row.get("review_label", "").strip().lower()
        if not review_id:
            raise ValueError("every human-review row requires review_id")
        if review_id in reviews:
            raise ValueError(f"duplicate human-review row: {review_id}")
        reviews[review_id] = label
    return reviews


def _load_review_key(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    review_ids = [str(row.get("review_id", "")).strip() for row in rows]
    if any(not review_id for review_id in review_ids):
        raise ValueError("every review-key row requires review_id")
    if len(review_ids) != len(set(review_ids)):
        raise ValueError("review-key rows must have unique review_id values")
    return rows


def load_evidence(evidence_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Path]]:
    paths = {
        "human_reviews": evidence_dir / "human-review.csv",
        "review_key": evidence_dir / "review-key.jsonl",
        "manifest": evidence_dir / "manifest.json",
        "preregistration": evidence_dir / "preregistration.md",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing CodeArchitect evidence: " + ", ".join(missing))

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    reviews = _load_reviews(paths["human_reviews"])
    key_rows = _load_review_key(paths["review_key"])
    key_ids = {row["review_id"] for row in key_rows}
    if set(reviews) != key_ids:
        missing_reviews = sorted(key_ids - set(reviews))
        extra_reviews = sorted(set(reviews) - key_ids)
        raise ValueError(
            "review/key identity mismatch; "
            f"missing={missing_reviews[:5]}, extra={extra_reviews[:5]}"
        )
    invalid = sorted(
        review_id for review_id, label in reviews.items() if label not in VALID_LABELS
    )
    if invalid:
        raise ValueError(
            f"human review is incomplete or invalid for {len(invalid)} row(s): "
            + ", ".join(invalid[:5])
        )
    if int(manifest["review_items"]) != len(key_rows):
        raise ValueError("manifest review_items does not match the review key")

    merged: list[dict[str, Any]] = []
    for row in key_rows:
        merged_row = dict(row)
        merged_row["label"] = reviews[row["review_id"]]
        merged.append(merged_row)
    return merged, manifest, paths


def summarize_policy(
    rows: list[dict[str, Any]], *, method: str, total: int, threshold: float | None = None
) -> dict[str, Any]:
    selected = []
    for row in rows:
        if method not in row["methods"]:
            continue
        if threshold is not None and float(row["semantic_similarity"]) < threshold:
            continue
        selected.append(row)
    correct = sum(row["label"] == "correct" for row in selected)
    correct_or_partial = sum(row["label"] in {"correct", "partial"} for row in selected)
    accepted = len(selected)
    return {
        "accepted": accepted,
        "coverage": accepted / total,
        "abstention": 1 - accepted / total,
        "strict_correct": correct,
        "strict_precision": correct / accepted if accepted else 0.0,
        "strict_precision_wilson_95": list(wilson(correct, accepted)),
        "correct_or_partial": correct_or_partial,
        "concept_fit_precision": correct_or_partial / accepted if accepted else 0.0,
    }


def build_experiment() -> Experiment:
    return Experiment.from_factors(
        key=EXPERIMENT_KEY,
        version=VERSION,
        factors=(
            Factor(
                "acceptance_threshold",
                (CONSERVATIVE_THRESHOLD, CANDIDATE_THRESHOLD),
                variable_type=VariableType.FLOAT,
            ),
        ),
        primary_metric="accepted_coverage",
        salt="codearchitect-threshold-evaluation-v1",
        hypothesis=(
            "Lowering the semantic acceptance threshold increases accepted coverage while "
            "preserving at least 80% strict human-reviewed precision and losing no more "
            "than five percentage points versus the conservative policy."
        ),
        metrics=(
            MetricDefinition("accepted_coverage", "binary", "primary"),
            MetricDefinition("strict_human_reviewed_precision", "binary", "secondary"),
            MetricDefinition("correct_or_partial_precision", "binary", "secondary"),
            MetricDefinition("abstention_rate", "binary", "secondary"),
        ),
        guardrails=(Guardrail("strict_human_reviewed_precision", MAXIMUM_PRECISION_DROP),),
        assignment_algorithm_version="offline-policy-replay-v1",
        policy_version="acceptance-threshold-v1",
        eligibility_rule="repository-disjoint held-out components",
        attribution_window_hours=1,
        stopping_rule="single frozen human review",
        statistical_method="blinded-human-adjudication-v1",
    )


def evaluate(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    total = int(manifest["holdout_components"])
    candidate = summarize_policy(
        rows, method="semantic_ast", threshold=CANDIDATE_THRESHOLD, total=total
    )
    conservative = summarize_policy(
        rows, method="semantic_ast", threshold=CONSERVATIVE_THRESHOLD, total=total
    )
    baseline = summarize_policy(rows, method="exact_token_ast", total=total)
    if candidate["accepted"] != int(manifest["semantic_accepts"]):
        raise ValueError("candidate accepts do not match the frozen manifest")
    if baseline["accepted"] != int(manifest["baseline_accepts"]):
        raise ValueError("exact-token accepts do not match the frozen manifest")

    precision_delta = candidate["strict_precision"] - conservative["strict_precision"]
    decision = evaluate_pre_registered_gates(
        change=CHANGE,
        evidence_complete=True,
        gates=(
            PreRegisteredGate(
                "candidate_coverage_at_least_20_percent",
                "accepted_coverage",
                candidate["coverage"],
                "at_least",
                MINIMUM_COVERAGE,
            ),
            PreRegisteredGate(
                "candidate_strict_precision_at_least_80_percent",
                "strict_human_reviewed_precision",
                candidate["strict_precision"],
                "at_least",
                MINIMUM_STRICT_PRECISION,
            ),
            PreRegisteredGate(
                "precision_drop_no_more_than_5_points",
                "candidate_minus_conservative_strict_precision",
                precision_delta,
                "at_least",
                -MAXIMUM_PRECISION_DROP,
            ),
        ),
    )
    return {
        "decision": decision.decision,
        "change": CHANGE,
        "evidence_status": decision.evidence_status,
        "eligible_for_decision": decision.eligible_for_decision,
        "gate_evaluations": [asdict(gate) for gate in decision.gates],
        "failed_gates": list(decision.failed_gates),
        "candidate_0_62": candidate,
        "conservative_0_72": conservative,
        "exact_token_baseline": baseline,
        "candidate_minus_conservative_strict_precision": precision_delta,
    }


def run_replay(
    *, evidence_dir: Path, registry_path: Path, output_path: Path, replace: bool = False
) -> dict[str, Any]:
    if registry_path.exists():
        if not replace:
            raise FileExistsError(f"registry already exists: {registry_path}")
        registry_path.unlink()
    if output_path.exists() and not replace:
        raise FileExistsError(f"output already exists: {output_path}")
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows, manifest, source_paths = load_evidence(evidence_dir)
    result = evaluate(rows, manifest)
    source_evidence = {
        name: {"path": str(path.resolve()), "sha256": sha256_file(path)}
        for name, path in source_paths.items()
    }
    decision_evidence = {
        "schema_version": "variantgrid-offline-policy-decision/v1",
        "study_type": "paired offline model-policy evaluation",
        "result": result,
        "source_evidence": source_evidence,
        "source_manifest": manifest,
        "limitations": [
            "The 151 components are repository-disjoint from taxonomy fitting, but earlier model judgments informed development.",
            "The human labels are new; this measures accepted-prediction quality, not downstream developer outcomes.",
            "The two threshold policies were replayed on the same held-out components, not randomly assigned subjects.",
        ],
    }

    experiment = build_experiment()
    with ExperimentRegistry(registry_path) as registry:
        registry.create(experiment)
        registry.transition(EXPERIMENT_KEY, VERSION, Lifecycle.RUNNING)
        record = registry.record_decision(
            EXPERIMENT_KEY,
            VERSION,
            str(result["decision"]),
            decision_evidence,
        )
        registry.transition(EXPERIMENT_KEY, VERSION, Lifecycle.STOPPED)
        registry_snapshot = registry.persistence_snapshot(EXPERIMENT_KEY, VERSION)

    snapshot = {
        "schema_version": "variantgrid-codearchitect-study/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reproduction_command": (
            "PYTHONPATH=src python3 studies/codearchitect-threshold-evaluation/"
            "run_evaluation.py --replace"
        ),
        "registry_file": {
            "path": str(registry_path.resolve()),
            "sha256": sha256_file(registry_path),
        },
        "decision_record": {
            "experiment_key": record.experiment_key,
            "version": record.version,
            "decision": record.decision,
            "created_at": record.created_at,
        },
        "variantgrid_registry": registry_snapshot,
        "result": result,
        "source_evidence": source_evidence,
    }
    output_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay the completed CodeArchitect threshold gate through VariantGrid."
    )
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument(
        "--registry", type=Path, default=STUDY_DIR / "variantgrid.sqlite"
    )
    parser.add_argument(
        "--output", type=Path, default=STUDY_DIR / "decision-snapshot.json"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace only this study's generated registry and snapshot.",
    )
    args = parser.parse_args()
    snapshot = run_replay(
        evidence_dir=args.evidence_dir,
        registry_path=args.registry,
        output_path=args.output,
        replace=args.replace,
    )
    print(json.dumps(snapshot["result"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
