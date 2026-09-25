from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import random
from statistics import mean, median
import subprocess
from time import perf_counter_ns

from variantgrid import AssignmentEngine, Experiment, VariantState, analyze_binary_metric


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks" / "results.json"


def hardware_environment() -> dict:
    """Return useful, best-effort host metadata without adding dependencies."""

    def sysctl(name: str) -> str | None:
        if platform.system() != "Darwin":
            return None
        try:
            value = subprocess.run(
                ["sysctl", "-n", name],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            return value or None
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None

    memory_bytes = sysctl("hw.memsize")
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_model": sysctl("machdep.cpu.brand_string"),
        "logical_cpu_count": os.cpu_count(),
        "memory_bytes": int(memory_bytes) if memory_bytes and memory_bytes.isdigit() else None,
    }


def experiment(weights: tuple[float, float] = (1.0, 1.0)) -> Experiment:
    return Experiment(
        key="activation-flow",
        version=1,
        states=(
            VariantState("control", {"experience": "control"}, weights[0]),
            VariantState("treatment", {"experience": "guided"}, weights[1]),
        ),
        primary_metric="activated",
        salt="variantgrid-public-benchmark-v2",
        minimum_sample_size=1_000,
    )


def allocation_benchmark(weights: tuple[float, float], total: int = 1_000_000) -> dict:
    engine = AssignmentEngine(experiment(weights))
    counts: Counter[str] = Counter(engine.assign(f"subject-{index}").key for index in range(total))
    weight_total = sum(weights)
    targets = {"control": weights[0] / weight_total, "treatment": weights[1] / weight_total}
    drift = {key: abs(counts[key] / total - target) * 100 for key, target in targets.items()}
    repeated_subjects = [f"subject-{index}" for index in range(10_000)]
    first = [engine.assign(subject).key for subject in repeated_subjects]
    second = [engine.assign(subject).key for subject in repeated_subjects]
    stickiness = sum(a == b for a, b in zip(first, second, strict=True)) / len(first)
    return {
        "assignments": total,
        "declared_weights": list(weights),
        "target_shares": targets,
        "allocation_counts": dict(sorted(counts.items())),
        "maximum_allocation_drift_percentage_points": round(max(drift.values()), 6),
        "repeat_subjects_checked": len(repeated_subjects),
        "stickiness_percent": round(stickiness * 100, 6),
    }


def latency_trial(total: int = 1_000_000, sample_every: int = 100) -> dict:
    engine = AssignmentEngine(experiment())
    sampled_latencies_ns: list[int] = []
    started = perf_counter_ns()
    for index in range(total):
        subject = f"latency-subject-{index}"
        if index % sample_every == 0:
            call_started = perf_counter_ns()
            engine.assign(subject)
            sampled_latencies_ns.append(perf_counter_ns() - call_started)
        else:
            engine.assign(subject)
    elapsed_ns = perf_counter_ns() - started
    ordered = sorted(sampled_latencies_ns)
    return {
        "assignments": total,
        "samples": len(ordered),
        "sample_every": sample_every,
        "sampled_p50_microseconds": round(ordered[int(len(ordered) * 0.50)] / 1_000, 3),
        "sampled_p95_microseconds": round(ordered[int(len(ordered) * 0.95)] / 1_000, 3),
        "sampled_p99_microseconds": round(ordered[int(len(ordered) * 0.99)] / 1_000, 3),
        "throughput_assignments_per_second": round(total / (elapsed_ns / 1_000_000_000), 0),
    }


def performance_benchmark(trials: int = 5) -> dict:
    results = [latency_trial() for _ in range(trials)]
    p95s = [trial["sampled_p95_microseconds"] for trial in results]
    throughputs = [trial["throughput_assignments_per_second"] for trial in results]
    return {
        "trial_count": trials,
        "trials": results,
        "median_trial_sampled_p95_microseconds": round(median(p95s), 3),
        "minimum_trial_sampled_p95_microseconds": min(p95s),
        "maximum_trial_sampled_p95_microseconds": max(p95s),
        "median_throughput_assignments_per_second": round(median(throughputs), 0),
    }


def aa_calibration(
    *, baseline_rate: float, seed: int, simulations: int = 10_000, subjects_per_variant: int = 1_000
) -> dict:
    rng = random.Random(seed)
    false_positives = 0
    intervals_covering_zero = 0
    effects: list[float] = []
    for _ in range(simulations):
        control = sum(rng.random() < baseline_rate for _ in range(subjects_per_variant))
        treatment = sum(rng.random() < baseline_rate for _ in range(subjects_per_variant))
        readout = analyze_binary_metric(
            control_exposed=subjects_per_variant,
            control_converted=control,
            treatment_exposed=subjects_per_variant,
            treatment_converted=treatment,
        )
        false_positives += readout.p_value < 0.05
        low, high = readout.confidence_interval_95
        intervals_covering_zero += low <= 0 <= high
        effects.append(readout.absolute_lift)
    false_positive_rate = false_positives / simulations * 100
    coverage = intervals_covering_zero / simulations * 100
    return {
        "baseline_rate_percent": baseline_rate * 100,
        "seed": seed,
        "simulations": simulations,
        "subjects_per_variant_per_simulation": subjects_per_variant,
        "false_positive_rate_percent": round(false_positive_rate, 3),
        "nominal_alpha_percent": 5.0,
        "confidence_interval_coverage_percent": round(coverage, 3),
        "nominal_confidence_interval_coverage_percent": 95.0,
        "mean_estimated_effect_percentage_points": round(mean(effects) * 100, 6),
        "within_predeclared_bounds": 4.5 <= false_positive_rate <= 5.5 and 94 <= coverage <= 96,
    }


def effect_recovery(simulations: int = 2_000, subjects_per_variant: int = 3_000) -> dict:
    rng = random.Random(24092026)
    injected = 0.02
    estimates: list[float] = []
    detections = 0
    for _ in range(simulations):
        control = sum(rng.random() < 0.10 for _ in range(subjects_per_variant))
        treatment = sum(rng.random() < 0.12 for _ in range(subjects_per_variant))
        readout = analyze_binary_metric(
            control_exposed=subjects_per_variant,
            control_converted=control,
            treatment_exposed=subjects_per_variant,
            treatment_converted=treatment,
        )
        estimates.append(readout.absolute_lift)
        detections += readout.p_value < 0.05 and readout.absolute_lift > 0
    average = mean(estimates)
    return {
        "seed": 24092026,
        "simulations": simulations,
        "subjects_per_variant_per_simulation": subjects_per_variant,
        "injected_effect_percentage_points": injected * 100,
        "mean_recovered_effect_percentage_points": round(average * 100, 6),
        "absolute_bias_percentage_points": round(abs(average - injected) * 100, 6),
        "positive_detection_rate_percent": round(detections / simulations * 100, 3),
    }


def main() -> None:
    allocations = {
        "50_50": allocation_benchmark((1, 1)),
        "80_20": allocation_benchmark((4, 1)),
        "90_10": allocation_benchmark((9, 1)),
    }
    calibration = {
        "1_percent": aa_calibration(baseline_rate=0.01, seed=2026092401),
        "10_percent_historical_fixture": aa_calibration(baseline_rate=0.10, seed=20260924),
        "50_percent": aa_calibration(baseline_rate=0.50, seed=2026092450),
    }
    performance = performance_benchmark()
    effect_recovery_result = effect_recovery()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology_version": "3.0",
        "environment": hardware_environment(),
        "predeclared_thresholds": {
            "stickiness_percent": 100.0,
            "maximum_allocation_drift_percentage_points": 0.25,
            "maximum_median_sampled_p95_microseconds": 25.0,
            "minimum_median_throughput_assignments_per_second": 50_000,
            "false_positive_rate_percent": [4.5, 5.5],
            "confidence_interval_coverage_percent": [94.0, 96.0],
            "maximum_effect_recovery_absolute_bias_percentage_points": 0.25,
        },
        "assignment_allocations": allocations,
        "assignment_performance": performance,
        "aa_calibration": calibration,
        "effect_recovery": effect_recovery_result,
        "regression_status": {
            "assignment": all(
                result["stickiness_percent"] == 100.0
                and result["maximum_allocation_drift_percentage_points"] <= 0.25
                for result in allocations.values()
            ),
            "statistical_calibration": all(
                result["within_predeclared_bounds"] for result in calibration.values()
            ),
            "local_assignment_performance": (
                performance["median_trial_sampled_p95_microseconds"] <= 25.0
                and performance["median_throughput_assignments_per_second"] >= 50_000
            ),
            "effect_recovery": effect_recovery_result[
                "absolute_bias_percentage_points"
            ] <= 0.25,
        },
    }
    RESULTS.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
