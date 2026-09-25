from __future__ import annotations

import json
from pathlib import Path


FINGERPRINT = "f" * 64
GENERATED_AT = "2026-09-24T12:00:00+00:00"


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_approved_evidence(root: Path) -> Path:
    """Create a compact, internally consistent evidence bundle for unit tests."""
    trials = [
        {
            "assignments": 1_000_000,
            "samples": 10_000,
            "sample_every": 100,
            "sampled_p50_microseconds": 1.375,
            "sampled_p95_microseconds": value,
            "sampled_p99_microseconds": 1.600,
            "throughput_assignments_per_second": 700_000.0,
        }
        for value in (1.416, 1.417, 1.958, 1.417, 1.458)
    ]
    allocations = {}
    for policy, weights, counts, drift in (
        ("50_50", [0.5, 0.5], {"control": 499_598, "treatment": 500_402}, 0.0402),
        ("80_20", [0.8, 0.2], {"control": 799_880, "treatment": 200_120}, 0.0120),
        ("90_10", [0.9, 0.1], {"control": 899_930, "treatment": 100_070}, 0.0070),
    ):
        allocations[policy] = {
            "assignments": 1_000_000,
            "declared_weights": weights,
            "target_shares": {"control": weights[0], "treatment": weights[1]},
            "allocation_counts": counts,
            "maximum_allocation_drift_percentage_points": drift,
            "repeat_subjects_checked": 1_000_000,
            "stickiness_percent": 100.0,
        }
    thresholds = {
        "false_positive_rate_percent": [4.5, 5.5],
        "confidence_interval_coverage_percent": [94.0, 96.0],
    }
    benchmark = {
        "generated_at": GENERATED_AT,
        "methodology_version": "3.0",
        "assignment_allocations": allocations,
        "assignment_performance": {
            "trial_count": 5,
            "median_trial_sampled_p95_microseconds": 1.417,
            "minimum_trial_sampled_p95_microseconds": 1.416,
            "maximum_trial_sampled_p95_microseconds": 1.958,
            "median_throughput_assignments_per_second": 700_000.0,
            "trials": trials,
        },
        "regression_status": {
            "assignment": True,
            "local_assignment_performance": True,
            "statistical_calibration": True,
            "effect_recovery": True,
        },
    }
    environment = {
        "cpu_model": None,
        "python_implementation": "CPython",
        "python": "3.13.2",
    }
    vg2_observed = {
        "policies": ["50_50", "80_20", "90_10"],
        "assignments_per_policy": 1_000_000,
        "maximum_drift_percentage_points": 0.0402,
        "median_trial_sampled_p95_microseconds": 1.417,
        "trial_count": 5,
    }
    common = {
        "status": "approved",
        "source_fingerprint_sha256": FINGERPRINT,
        "reproduction_command": "PYTHONPATH=src python3 scripts/verify_release.py",
        "generated_artifacts": [
            "benchmarks/results.json",
            "artifacts/release_audit.json",
            "artifacts/test-results.txt",
        ],
        "environment": environment,
        "predeclared_thresholds": thresholds,
    }
    vg1 = {
        **common,
        "claim_id": "VG-1",
        "wording": "Typed variants and local integrity gates produce trustworthy local readouts.",
        "observed": {"required_tests_present": ["one", "two"], "assignment_gate": True},
        "limitations": "Local implementation evidence only.",
    }
    vg2 = {
        **common,
        "claim_id": "VG-2R",
        "wording": "Fresh local assignment benchmark wording.",
        "observed": vg2_observed,
        "limitations": "Local in-process performance is hardware- and load-sensitive.",
    }
    categories = {
        name: {"present": True, "test": f"test_{name}"}
        for name in ("duplicate", "missing", "late", "reordered", "cross_version")
    }
    vg3 = {
        **common,
        "claim_id": "VG-3",
        "wording": "Seeded calibration and five local integrity categories passed.",
        "observed": {
            "false_positive_rate_percent": 5.13,
            "interval_coverage_percent": 94.87,
            "simulations": 10_000,
            "event_categories": categories,
        },
        "limitations": "Synthetic simulation and local SQLite evidence only.",
    }
    manifest = {
        "generated_at": GENERATED_AT,
        "source_fingerprint_sha256": FINGERPRINT,
        "release_status": "local_demo_ready",
        "claims": {"VG-1": "approved", "VG-2": "blocked", "VG-2R": "approved", "VG-3": "approved"},
    }
    audit = {
        "source": {
            "source_fingerprint_sha256": FINGERPRINT,
            "benchmark_generated_at": GENERATED_AT,
        },
        "benchmark": {"successful": True, "results": "benchmarks/results.json"},
        "claims": {"VG-1": vg1, "VG-2R": vg2, "VG-3": vg3},
    }
    _write(root / "benchmarks" / "results.json", benchmark)
    _write(root / "artifacts" / "release_manifest.json", manifest)
    _write(root / "artifacts" / "release_audit.json", audit)
    _write(root / "artifacts" / "claims" / "VG-1.json", vg1)
    _write(root / "artifacts" / "claims" / "VG-2R.json", vg2)
    _write(root / "artifacts" / "claims" / "VG-3.json", vg3)
    return root
