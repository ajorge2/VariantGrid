from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks" / "results.json"
ARTIFACTS = ROOT / "artifacts"
AUDIT = ARTIFACTS / "release_audit.json"
MANIFEST = ARTIFACTS / "release_manifest.json"
TEST_LOG = ARTIFACTS / "test-results.txt"
BENCHMARK_LOG = ARTIFACTS / "benchmark-output.txt"
CLAIM_DIR = ARTIFACTS / "claims"
PUBLIC_COMMAND = "PYTHONPATH=src python3 scripts/verify_release.py"


VG1_TESTS = {
    "test_typed_conditional_variables_and_rejection_reasons_are_explicit",
    "test_canonical_ids_ignore_mapping_insertion_order",
    "test_file_round_trip_replays_identical_order_ids_and_assignment_inputs",
    "test_launched_version_is_immutable_even_through_direct_sql",
    "test_repeat_assignment_is_fully_sticky",
    "test_exact_and_concurrent_duplicates_never_double_count",
    "test_missing_exposure_and_pre_exposure_goal_are_excluded",
    "test_integrity_failures_withhold_effects_and_block_decisions",
}

VG3_EVENT_TESTS = {
    "duplicate": "test_exact_and_concurrent_duplicates_never_double_count",
    "missing": "test_missing_exposure_and_pre_exposure_goal_are_excluded",
    "late": "test_late_events_use_event_time_and_window_endpoint_is_inclusive",
    "reordered": "test_reordered_batches_reconcile_to_the_same_digest",
    "cross_version": "test_versions_are_validated_and_never_cross_attribution",
}

POSTGRES_TESTS = {
    "test_contract_identical_insert_is_idempotent_and_collision_is_visible",
    "test_contract_batch_result_is_recoverable_and_batch_identity_is_immutable",
    "test_contract_batch_quarantine_and_dead_letter_lifecycle_are_durable",
    "test_contract_event_and_receipt_order_are_independently_queryable",
    "test_concurrent_duplicate_delivery_across_independent_connections",
    "test_batch_retry_and_durable_reopen_recover_one_committed_result",
    "test_dead_letter_replay_survives_repository_reopen",
}

WORKSTREAM_TESTS = {
    "A_registry": {
        "test_typed_conditional_variables_and_rejection_reasons_are_explicit",
        "test_file_round_trip_replays_identical_order_ids_and_assignment_inputs",
        "test_launched_version_is_immutable_even_through_direct_sql",
        "test_material_edit_clones_to_new_draft_and_preserves_source",
        "test_invalid_lifecycle_transitions_fail_visibly",
        "test_v1_database_upgrades_rolls_back_and_reapplies_without_losing_json",
    },
    "B_assignment": {
        "test_repeat_assignment_is_fully_sticky",
        "test_version_salt_policy_and_experiment_namespaces_are_isolated",
        "test_ineligibility_and_unknown_rules_fail_closed_with_immutable_defaults",
        "test_frozen_fixtures_match_engine_service_and_local_sdk",
    },
    "C_ingestion": set(VG3_EVENT_TESTS.values())
    | {
        "test_retry_after_timeout_recovers_the_committed_batch_result",
        "test_dead_letter_can_be_replayed_after_version_catalog_changes",
        "test_contract_identical_insert_is_idempotent_and_collision_is_visible",
    },
    "D_inference": {
        "test_analytical_binary_power_agrees_with_seeded_simulation",
        "test_binary_recovers_positive_zero_and_negative_effects",
        "test_continuous_recovers_positive_zero_and_negative_effects",
        "test_srm_injection_suppresses_recommendation",
        "test_guardrail_direction_threshold_and_decision_suppression",
        "test_repeated_unadjusted_peeking_warns_and_suppresses_recommendation",
    },
    "E_api_sdk": {
        "test_service_and_local_evaluator_have_complete_assignment_parity",
        "test_get_records_only_one_exposure_per_context",
        "test_outage_returns_visible_safe_default",
        "test_timeouts_are_bounded_for_assignment_and_tracking",
        "test_retry_after_lost_ack_does_not_double_count",
    },
    "F_dashboard": {
        "test_launch_requires_explicit_review_and_freezes_configuration",
        "test_health_is_rendered_before_effect_and_every_chart_is_labeled",
        "test_integrity_failures_withhold_effects_and_block_decisions",
        "test_decision_preserves_full_immutable_evidence_snapshot",
        "test_lifecycle_controls_preserve_history_and_rollback",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(cwd / "src")
    environment["PYTHONWARNINGS"] = "error::ResourceWarning"
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def source_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    included = (
        root / "src",
        root / "tests",
        root / "scripts",
        root / "benchmarks" / "run_benchmarks.py",
        root / "pyproject.toml",
    )
    files: list[Path] = []
    for candidate in included:
        if candidate.is_dir():
            files.extend(
                path
                for path in candidate.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
            )
        elif candidate.is_file():
            files.append(candidate)
    for path in sorted(set(files), key=lambda value: str(value.relative_to(root))):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_revision(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def parse_test_names(output: str) -> list[str]:
    names = re.findall(r"^(test_[A-Za-z0-9_]+) \(", output, flags=re.MULTILINE)
    return sorted(set(names))


def parse_test_count(output: str) -> int:
    matches = re.findall(r"Ran (\d+) tests?", output)
    return int(matches[-1]) if matches else 0


def all_true(mapping: dict[str, Any]) -> bool:
    return all(bool(value) for value in mapping.values())


def workstream_audit(
    test_set: set[str], benchmark: dict[str, Any], tests_ok: bool, postgres_verified: bool
) -> dict[str, Any]:
    audit: dict[str, Any] = {}
    for name, required in WORKSTREAM_TESTS.items():
        missing = sorted(required - test_set)
        status = "pass" if tests_ok and not missing else "fail"
        audit[name] = {
            "local_acceptance": status,
            "required_tests": sorted(required),
            "missing_tests": missing,
        }

    regression = benchmark.get("regression_status", {})
    if not regression.get("assignment", False) or not regression.get(
        "local_assignment_performance", False
    ):
        audit["B_assignment"]["local_acceptance"] = "fail"
    if not regression.get("statistical_calibration", False) or not regression.get(
        "effect_recovery", False
    ):
        audit["D_inference"]["local_acceptance"] = "fail"

    audit["C_ingestion"].update(
        {
            "plan_acceptance": "pass" if postgres_verified else "partial",
            "postgres_integration_verified": postgres_verified,
            "production_gap": (
                "A real Postgres adapter passes transactional and 16-connection contention tests; "
                "sustained load, failover, multi-region behavior, and production authorization remain unproven."
                if postgres_verified
                else "Postgres integration was not enabled for this release run."
            ),
        }
    )
    audit["E_api_sdk"].update(
        {
            "plan_acceptance": "local_demo_pass",
            "production_gap": "The HTTP adapter and async queue are local/demo implementations without production authentication, durable queueing, or network SLO evidence.",
        }
    )
    audit["F_dashboard"].update(
        {
            "plan_acceptance": "local_demo_pass",
            "production_gap": "The operator sandbox is process-local and uses explicit fixtures rather than a deployed authenticated control plane.",
        }
    )
    audit["G_evidence"] = {
        "local_acceptance": "pass" if tests_ok and all_true(regression) else "fail",
        "plan_acceptance": "local_demo_pass",
        "production_gap": "No deployed service-level telemetry, load/chaos evidence, or production performance dashboard.",
    }
    return audit


def build_claims(
    *, tests_ok: bool, test_set: set[str], benchmark: dict[str, Any]
) -> dict[str, Any]:
    allocations = benchmark["assignment_allocations"]
    fifty = allocations["50_50"]
    performance = benchmark["assignment_performance"]
    historical = benchmark["aa_calibration"]["10_percent_historical_fixture"]
    regression = benchmark["regression_status"]
    p95 = performance["median_trial_sampled_p95_microseconds"]
    max_drift = max(
        value["maximum_allocation_drift_percentage_points"] for value in allocations.values()
    )
    vg1_ok = tests_ok and VG1_TESTS <= test_set and regression["assignment"]
    event_evidence = {
        category: {"test": test, "present": test in test_set}
        for category, test in VG3_EVENT_TESTS.items()
    }
    vg3_ok = (
        tests_ok
        and all(item["present"] for item in event_evidence.values())
        and regression["statistical_calibration"]
        and regression["effect_recovery"]
        and historical["false_positive_rate_percent"] == 5.13
        and historical["confidence_interval_coverage_percent"] == 94.87
    )
    historical_exact = (
        fifty["stickiness_percent"] == 100.0
        and round(fifty["maximum_allocation_drift_percentage_points"], 2) <= 0.06
        and round(p95, 2) == 1.46
    )
    return {
        "VG-1": {
            "wording": "Typed multivariate variants, constraint-aware states, deterministic versioned assignment, and idempotent exposure logging produce trustworthy local readouts.",
            "status": "approved" if vg1_ok else "blocked",
            "observed": {"required_tests_present": sorted(VG1_TESTS), "assignment_gate": regression["assignment"]},
            "limitations": "Approved for the local implementation and its tested semantics; it is not evidence of production adoption, distributed durability, uptime, or business impact.",
        },
        "VG-2": {
            "wording": "Preserved 100% repeat-user stickiness across 1M simulated assignments with 0.06 percentage-point allocation drift and 1.46 microsecond sampled p95 local assignment latency.",
            "status": "approved" if historical_exact else "blocked",
            "observed": {
                "stickiness_percent": fifty["stickiness_percent"],
                "50_50_allocation_drift_percentage_points": fifty[
                    "maximum_allocation_drift_percentage_points"
                ],
                "maximum_drift_across_policies_percentage_points": max_drift,
                "median_trial_sampled_p95_microseconds": p95,
                "trial_range_microseconds": [
                    performance["minimum_trial_sampled_p95_microseconds"],
                    performance["maximum_trial_sampled_p95_microseconds"],
                ],
            },
            "limitations": "The exact 1.46 microsecond historical latency did not reproduce. This is an in-process Python microbenchmark, not service latency.",
        },
        "VG-2R": {
            "wording": (
                f"Preserved 100% repeat-user stickiness across three 1M-assignment policies "
                f"with at most {max_drift:.4f} percentage-point drift and a {p95:.3f} microsecond "
                "median sampled p95 local assignment latency across five trials."
            ),
            "status": (
                "approved"
                if tests_ok
                and regression["assignment"]
                and regression["local_assignment_performance"]
                else "blocked"
            ),
            "observed": {
                "policies": sorted(allocations),
                "assignments_per_policy": min(value["assignments"] for value in allocations.values()),
                "maximum_drift_percentage_points": max_drift,
                "median_trial_sampled_p95_microseconds": p95,
                "trial_count": performance["trial_count"],
            },
            "limitations": "Fresh replacement wording for this machine and run; local in-process performance is hardware- and load-sensitive.",
        },
        "VG-3": {
            "wording": "Calibrated a 5.13% false-positive rate and 94.87% interval coverage across 10K A/A simulations; passed five adversarial integrity categories spanning duplicate, missing, late, reordered, and cross-version events.",
            "status": "approved" if vg3_ok else "blocked",
            "observed": {
                "false_positive_rate_percent": historical["false_positive_rate_percent"],
                "interval_coverage_percent": historical[
                    "confidence_interval_coverage_percent"
                ],
                "simulations": historical["simulations"],
                "expanded_baseline_matrix_pass": regression["statistical_calibration"],
                "event_categories": event_evidence,
            },
            "limitations": "Seeded simulations and local SQLite integrity tests do not establish live causal impact, Postgres behavior, or production reliability.",
        },
    }


def claim_manifest(
    claim_id: str,
    claim: dict[str, Any],
    *,
    fingerprint: str,
    revision: str | None,
    benchmark: dict[str, Any],
) -> dict[str, Any]:
    owners = {
        "VG-1": "registry engineer with assignment and ingestion sign-off",
        "VG-2": "assignment engineer",
        "VG-2R": "assignment engineer",
        "VG-3": "inference integrity engineer with ingestion sign-off",
    }
    fixtures: dict[str, Any] = {
        "VG-1": "unit/integration fixtures and assignment benchmark salt variantgrid-public-benchmark-v2",
        "VG-2": "subject-{0..999999}; five latency trials; sample every 100 calls",
        "VG-2R": "subject-{0..999999}; five latency trials; sample every 100 calls",
        "VG-3": {"seeds": [2026092401, 20260924, 2026092450, 24092026]},
    }
    return {
        "claim_id": claim_id,
        "wording": claim["wording"],
        "primary_owner": owners[claim_id],
        "independent_signer": "evidence/release engineer",
        "source_revision": revision,
        "source_fingerprint_sha256": fingerprint,
        "reproduction_command": PUBLIC_COMMAND,
        "generated_artifacts": [
            "benchmarks/results.json",
            "artifacts/release_audit.json",
            "artifacts/test-results.txt",
        ],
        "fixture_or_seed": fixtures[claim_id],
        "environment": benchmark["environment"],
        "predeclared_thresholds": benchmark["predeclared_thresholds"],
        "observed": claim["observed"],
        "limitations": claim["limitations"],
        "status": claim["status"],
    }


def verify_in_place() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    CLAIM_DIR.mkdir(parents=True, exist_ok=True)

    test_process = run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT
    )
    test_output = test_process.stdout + test_process.stderr
    TEST_LOG.write_text(test_output)
    test_names = parse_test_names(test_output)
    test_count = parse_test_count(test_output)
    test_set = set(test_names)
    tests_ok = test_process.returncode == 0
    postgres_requested = os.environ.get("VARIANTGRID_RUN_POSTGRES_TESTS") == "1"
    postgres_verified = postgres_requested and tests_ok and POSTGRES_TESTS <= test_set

    benchmark_process = run([sys.executable, "benchmarks/run_benchmarks.py"], cwd=ROOT)
    benchmark_output = benchmark_process.stdout + benchmark_process.stderr
    BENCHMARK_LOG.write_text(benchmark_output)
    benchmark_ok = benchmark_process.returncode == 0 and RESULTS.exists()
    benchmark = json.loads(RESULTS.read_text()) if benchmark_ok else {}

    if not benchmark_ok:
        payload = {
            "generated_at": utc_now(),
            "release_status": "blocked",
            "reason": "Benchmark execution failed or produced no results artifact.",
            "tests": {"successful": tests_ok, "run": test_count, "names": test_names},
            "benchmark_return_code": benchmark_process.returncode,
        }
        AUDIT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    fingerprint = source_fingerprint(ROOT)
    revision = git_revision(ROOT)
    claims = build_claims(tests_ok=tests_ok, test_set=test_set, benchmark=benchmark)
    workstreams = workstream_audit(test_set, benchmark, tests_ok, postgres_verified)
    regression = benchmark["regression_status"]
    local_gate = tests_ok and all_true(regression) and all(
        value["local_acceptance"] == "pass" for value in workstreams.values()
    )

    payload = {
        "generated_at": utc_now(),
        "methodology_version": "1.0",
        "release_status": "local_demo_ready" if local_gate else "blocked",
        "production_readiness": "not_proven",
        "reproduction_command": PUBLIC_COMMAND,
        "source": {
            "git_revision": revision,
            "source_fingerprint_sha256": fingerprint,
            "python": platform.python_version(),
            "benchmark_generated_at": benchmark["generated_at"],
            "benchmark_methodology_version": benchmark["methodology_version"],
        },
        "tests": {
            "command": "PYTHONPATH=src python3 -m unittest discover -s tests -v",
            "run": test_count,
            "successful": tests_ok,
            "return_code": test_process.returncode,
            "names": test_names,
            "raw_output": "artifacts/test-results.txt",
            "postgres_integration_requested": postgres_requested,
            "postgres_integration_verified": postgres_verified,
            "postgres_required_tests": sorted(POSTGRES_TESTS),
        },
        "benchmark": {
            "command": "PYTHONPATH=src python3 benchmarks/run_benchmarks.py",
            "successful": benchmark_ok and all_true(regression),
            "regression_status": regression,
            "results": "benchmarks/results.json",
            "raw_output": "artifacts/benchmark-output.txt",
        },
        "claims": claims,
        "workstreams": workstreams,
        "phase_readiness": {
            "phase_0_baseline": "pass_with_historical_VG-2_wording_blocked",
            "phase_1_trustworthy_backend": (
                "pass_local_postgres_integration" if postgres_verified else "partial_until_exercised_Postgres_adapter"
            ),
            "phase_2_engineer_usability": "local_demo_pass",
            "phase_3_harden_and_demonstrate": "partial_no_deployment_load_chaos_or_production_observability",
            "phase_4_adaptive_allocation": "not_started_by_design",
            "phase_5_large_space_optimization": "not_started_by_design",
        },
        "limitations": [
            "No customer adoption, revenue lift, uptime, or live causal impact is claimed.",
            (
                "Postgres is exercised locally, but sustained load, failover, multi-region behavior, and production authorization remain unproven."
                if postgres_verified
                else "No exercised Postgres integration is present in this release run."
            ),
            "The API, SDK queue, and dashboard are local demonstration components rather than a deployed production control plane.",
            "Local latency is hardware- and load-sensitive and does not represent HTTP service latency.",
        ],
    }
    AUDIT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    for claim_id, claim in claims.items():
        manifest = claim_manifest(
            claim_id,
            claim,
            fingerprint=fingerprint,
            revision=revision,
            benchmark=benchmark,
        )
        (CLAIM_DIR / f"{claim_id}.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )

    manifest = {
        "generated_at": payload["generated_at"],
        "source_fingerprint_sha256": fingerprint,
        "release_status": payload["release_status"],
        "claims": {claim_id: claim["status"] for claim_id, claim in claims.items()},
        "artifacts": [
            "artifacts/release_audit.json",
            "artifacts/release_manifest.json",
            "artifacts/test-results.txt",
            "artifacts/benchmark-output.txt",
            "artifacts/claims/VG-1.json",
            "artifacts/claims/VG-2.json",
            "artifacts/claims/VG-2R.json",
            "artifacts/claims/VG-3.json",
            "benchmarks/results.json",
        ],
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"release_status": payload["release_status"], "claims": manifest["claims"]}, indent=2))
    return 0 if local_gate else 1


def stage_clean_copy() -> int:
    with tempfile.TemporaryDirectory(prefix="variantgrid-release-") as directory:
        staged = Path(directory) / "variantgrid"
        shutil.copytree(
            ROOT,
            staged,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "artifacts"),
        )
        staged_results = staged / "benchmarks" / "results.json"
        staged_results.unlink(missing_ok=True)
        process = run([sys.executable, "scripts/verify_release.py", "--in-place"], cwd=staged)
        if process.stdout:
            print(process.stdout, end="")
        if process.stderr:
            print(process.stderr, file=sys.stderr, end="")

        generated = [
            "benchmarks/results.json",
            "artifacts/release_audit.json",
            "artifacts/release_manifest.json",
            "artifacts/test-results.txt",
            "artifacts/benchmark-output.txt",
            "artifacts/claims/VG-1.json",
            "artifacts/claims/VG-2.json",
            "artifacts/claims/VG-2R.json",
            "artifacts/claims/VG-3.json",
        ]
        for relative in generated:
            source = staged / relative
            if source.exists():
                destination = ROOT / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        return process.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reproduce VariantGrid tests, benchmarks, claim manifests, and release gates."
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Run directly in this tree. The default stages a clean checkout-like temporary copy.",
    )
    arguments = parser.parse_args()
    return verify_in_place() if arguments.in_place else stage_clean_copy()


if __name__ == "__main__":
    raise SystemExit(main())
