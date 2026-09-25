from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from .assignment import AssignmentEngine, AssignmentResult
from .models import Experiment


ASSIGNMENT_LAB_SCHEMA_VERSION = "1"
_REQUIRED_POLICIES = frozenset({"50_50", "80_20", "90_10"})


class AssignmentEvidenceError(ValueError):
    """Raised when generated assignment evidence is missing or inconsistent."""


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise AssignmentEvidenceError(f"Could not read generated artifact {path}: {error}") from error
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssignmentEvidenceError(f"Generated artifact is not valid JSON: {path}") from error
    if not isinstance(parsed, dict):
        raise AssignmentEvidenceError(f"Generated artifact must contain a JSON object: {path}")
    return parsed


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _number(value: Any, label: str) -> int | float:
    if type(value) not in (int, float):
        raise AssignmentEvidenceError(f"{label} must be numeric")
    return value


def load_assignment_evidence(project_root: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the approved VG-2R generated evidence.

    Values are deliberately read from release artifacts on every call. The lab
    contains no copied benchmark number that could drift from the signed run.
    """

    root = Path(project_root) if project_root is not None else _default_project_root()
    paths = {
        "claim": root / "artifacts" / "claims" / "VG-2R.json",
        "benchmark": root / "benchmarks" / "results.json",
        "manifest": root / "artifacts" / "release_manifest.json",
        "release_audit": root / "artifacts" / "release_audit.json",
    }
    claim = _read_json(paths["claim"])
    benchmark = _read_json(paths["benchmark"])
    manifest = _read_json(paths["manifest"])
    release_audit = _read_json(paths["release_audit"])

    if claim.get("claim_id") != "VG-2R" or claim.get("status") != "approved":
        raise AssignmentEvidenceError("VG-2R is not an approved generated claim")
    if (manifest.get("claims") or {}).get("VG-2R") != "approved":
        raise AssignmentEvidenceError("Release manifest does not approve VG-2R")
    audited_claim = (release_audit.get("claims") or {}).get("VG-2R", {})
    if audited_claim.get("status") != "approved":
        raise AssignmentEvidenceError("Release audit does not approve VG-2R")
    audited_benchmark = release_audit.get("benchmark") or {}
    if not audited_benchmark.get("successful") or audited_benchmark.get("results") != "benchmarks/results.json":
        raise AssignmentEvidenceError("Release audit did not verify the benchmark artifact")
    claim_fingerprint = claim.get("source_fingerprint_sha256")
    manifest_fingerprint = manifest.get("source_fingerprint_sha256")
    if not claim_fingerprint or claim_fingerprint != manifest_fingerprint:
        raise AssignmentEvidenceError("Claim and release manifest source fingerprints disagree")
    if (release_audit.get("source") or {}).get("source_fingerprint_sha256") != claim_fingerprint:
        raise AssignmentEvidenceError("Release audit source fingerprint disagrees with the claim")
    if (release_audit.get("source") or {}).get("benchmark_generated_at") != benchmark.get("generated_at"):
        raise AssignmentEvidenceError("Release audit and benchmark generation timestamps disagree")
    if "benchmarks/results.json" not in claim.get("generated_artifacts", []):
        raise AssignmentEvidenceError("VG-2R does not cite the benchmark artifact")

    allocations = benchmark.get("assignment_allocations")
    if not isinstance(allocations, Mapping) or set(allocations) != _REQUIRED_POLICIES:
        raise AssignmentEvidenceError("Benchmark must contain exactly 50/50, 80/20, and 90/10 policies")

    allocation_rows: list[dict[str, Any]] = []
    for policy in sorted(_REQUIRED_POLICIES):
        result = allocations[policy]
        if not isinstance(result, Mapping):
            raise AssignmentEvidenceError(f"Allocation evidence for {policy} must be an object")
        assignments = _number(result.get("assignments"), f"{policy} assignments")
        stickiness = _number(result.get("stickiness_percent"), f"{policy} stickiness")
        drift = _number(
            result.get("maximum_allocation_drift_percentage_points"),
            f"{policy} allocation drift",
        )
        if assignments < 1_000_000:
            raise AssignmentEvidenceError(f"{policy} has fewer than one million assignments")
        if stickiness != 100.0:
            raise AssignmentEvidenceError(f"{policy} does not demonstrate 100% repeat stickiness")
        allocation_rows.append(
            {
                "policy": policy,
                "assignments": assignments,
                "declared_weights": list(result.get("declared_weights", [])),
                "target_shares": dict(result.get("target_shares", {})),
                "allocation_counts": dict(result.get("allocation_counts", {})),
                "maximum_drift_percentage_points": drift,
                "repeat_subjects_checked": result.get("repeat_subjects_checked"),
                "stickiness_percent": stickiness,
            }
        )

    observed = claim.get("observed")
    if not isinstance(observed, Mapping):
        raise AssignmentEvidenceError("VG-2R observed evidence is missing")
    observed_policies = set(observed.get("policies", []))
    if observed_policies != _REQUIRED_POLICIES:
        raise AssignmentEvidenceError("VG-2R approved policy set disagrees with the benchmark")
    if observed.get("assignments_per_policy") != min(row["assignments"] for row in allocation_rows):
        raise AssignmentEvidenceError("VG-2R assignment count disagrees with the benchmark")
    maximum_drift = max(row["maximum_drift_percentage_points"] for row in allocation_rows)
    if observed.get("maximum_drift_percentage_points") != maximum_drift:
        raise AssignmentEvidenceError("VG-2R drift disagrees with the benchmark")

    performance = benchmark.get("assignment_performance")
    if not isinstance(performance, Mapping):
        raise AssignmentEvidenceError("Assignment performance evidence is missing")
    trials = performance.get("trials")
    if not isinstance(trials, list) or not trials:
        raise AssignmentEvidenceError("Latency evidence must contain repeated trials")
    trial_count = performance.get("trial_count")
    if trial_count != len(trials) or observed.get("trial_count") != trial_count:
        raise AssignmentEvidenceError("VG-2R trial count disagrees with the benchmark")
    median_p95 = _number(
        performance.get("median_trial_sampled_p95_microseconds"),
        "median sampled p95 latency",
    )
    if observed.get("median_trial_sampled_p95_microseconds") != median_p95:
        raise AssignmentEvidenceError("VG-2R latency disagrees with the benchmark")

    latency_trials: list[dict[str, Any]] = []
    required_trial_fields = (
        "assignments",
        "samples",
        "sample_every",
        "sampled_p50_microseconds",
        "sampled_p95_microseconds",
        "sampled_p99_microseconds",
        "throughput_assignments_per_second",
    )
    for index, trial in enumerate(trials, start=1):
        if not isinstance(trial, Mapping) or any(field not in trial for field in required_trial_fields):
            raise AssignmentEvidenceError(f"Latency trial {index} is incomplete")
        latency_trials.append({"trial": index, **{field: trial[field] for field in required_trial_fields}})

    regression = benchmark.get("regression_status")
    if not isinstance(regression, Mapping) or not (
        regression.get("assignment") and regression.get("local_assignment_performance")
    ):
        raise AssignmentEvidenceError("Assignment benchmark regression gates did not pass")
    limitation = claim.get("limitations")
    if not isinstance(limitation, str) or not limitation.strip():
        raise AssignmentEvidenceError("Approved claim must state its limitations")

    relative_paths = {name: str(path.relative_to(root)) for name, path in paths.items()}
    return {
        "available": True,
        "claim_id": claim["claim_id"],
        "status": claim["status"],
        "wording": claim.get("wording"),
        "allocation_policies": allocation_rows,
        "latency": {
            "scope": "local_in_process",
            "trial_count": trial_count,
            "median_sampled_p95_microseconds": median_p95,
            "minimum_sampled_p95_microseconds": performance.get(
                "minimum_trial_sampled_p95_microseconds"
            ),
            "maximum_sampled_p95_microseconds": performance.get(
                "maximum_trial_sampled_p95_microseconds"
            ),
            "median_throughput_assignments_per_second": performance.get(
                "median_throughput_assignments_per_second"
            ),
            "trials": latency_trials,
        },
        "provenance": {
            "generated_at": benchmark.get("generated_at"),
            "release_generated_at": manifest.get("generated_at"),
            "methodology_version": benchmark.get("methodology_version"),
            "source_fingerprint_sha256": claim_fingerprint,
            "reproduction_command": claim.get("reproduction_command"),
            "environment": dict(claim.get("environment", {})),
            "artifacts": relative_paths,
            "artifact_sha256": {name: _digest(path) for name, path in paths.items()},
        },
        "limitations": [limitation],
    }


def _assignment_record(result: AssignmentResult) -> dict[str, Any]:
    diagnostics = result.diagnostics
    return {
        "experiment_key": result.experiment_key,
        "version": result.experiment_version,
        "algorithm_version": result.algorithm_version,
        "policy_version": result.policy_version,
        "state_id": result.state_id,
        "state_key": result.state.key if result.state is not None else None,
        "values": dict(result.state.values) if result.state is not None else {},
        "probability": result.assignment_probability,
        "bucket": diagnostics.bucket if diagnostics is not None else None,
        "interval": {
            "start": diagnostics.interval_start if diagnostics is not None else None,
            "end": diagnostics.interval_end if diagnostics is not None else None,
        },
        "eligible": result.eligible,
        "reason": result.reason,
    }


class AssignmentLab:
    """Deterministic, serializable assignment proof for a dashboard surface."""

    def __init__(
        self,
        current_experiment: Experiment,
        comparison_experiment: Experiment,
        project_root: str | Path | None = None,
    ) -> None:
        if current_experiment.key != comparison_experiment.key:
            raise ValueError("Version-isolation proof requires the same experiment key")
        if current_experiment.version == comparison_experiment.version:
            raise ValueError("Version-isolation proof requires two different versions")
        self.current_experiment = current_experiment
        self.comparison_experiment = comparison_experiment
        self.project_root = project_root

    def snapshot(self, subject_id: str) -> dict[str, Any]:
        if not isinstance(subject_id, str) or not subject_id.strip():
            raise ValueError("subject_id is required")
        current_engine = AssignmentEngine(self.current_experiment)
        first = current_engine.assign_with_metadata(subject_id)
        repeated = current_engine.assign_with_metadata(subject_id)
        comparison = AssignmentEngine(self.comparison_experiment).assign_with_metadata(subject_id)

        first_record = _assignment_record(first)
        repeated_record = _assignment_record(repeated)
        comparison_record = _assignment_record(comparison)
        namespace_changed = self.current_experiment.version != self.comparison_experiment.version
        state_changed = first.state_id != comparison.state_id
        bucket_changed = first_record["bucket"] != comparison_record["bucket"]

        return {
            "schema_version": ASSIGNMENT_LAB_SCHEMA_VERSION,
            "subject_id": subject_id,
            "experiment_key": self.current_experiment.key,
            "same_version": {
                "identical": first_record == repeated_record,
                "first": first_record,
                "repeat": repeated_record,
            },
            "version_isolation": {
                "isolated": namespace_changed,
                "namespace_changed": namespace_changed,
                "bucket_changed": bucket_changed,
                "state_changed": state_changed,
                "interpretation": (
                    "Version is part of the hash namespace. A version change creates an independent "
                    "deterministic draw; landing in the same state is valid and is not evidence of leakage."
                ),
                "comparison": comparison_record,
            },
            "benchmark_evidence": load_assignment_evidence(self.project_root),
        }


def assignment_lab_snapshot(
    subject_id: str,
    current_experiment: Experiment,
    comparison_experiment: Experiment,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Functional wrapper for UI and API adapters."""

    return AssignmentLab(
        current_experiment,
        comparison_experiment,
        project_root,
    ).snapshot(subject_id)
