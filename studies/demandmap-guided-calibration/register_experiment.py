from __future__ import annotations

import argparse
import json
from pathlib import Path

from variantgrid.analysis import required_sample_size
from variantgrid.models import Experiment, Factor, Guardrail, MetricDefinition
from variantgrid.registry import ExperimentRegistry, Lifecycle, serialize_experiment


EXPERIMENT_KEY = "demandmap_guided_calibration"
VERSION = 1
BASELINE_RATE = 0.40
ABSOLUTE_EFFECT = 0.25
ALPHA = 0.05
POWER = 0.80


def planned_sample_size_per_variant() -> int:
    return required_sample_size(
        baseline_rate=BASELINE_RATE,
        absolute_effect=ABSOLUTE_EFFECT,
        alpha=ALPHA,
        power=POWER,
    )


def build_experiment() -> Experiment:
    return Experiment.from_factors(
        key=EXPERIMENT_KEY,
        version=VERSION,
        factors=(Factor("calibration_mode", ("self_serve", "guided")),),
        primary_metric="research_completed",
        salt="demandmap-guided-calibration-v1",
        hypothesis=(
            "A guided evidence-review workflow increases ten-minute market-research "
            "completion without reducing successful research requests by more than "
            "five percentage points."
        ),
        metrics=(
            MetricDefinition("research_completed", "binary", "primary"),
            MetricDefinition("research_request_success", "binary", "secondary"),
            MetricDefinition("evidence_reviewed_before_research", "binary", "secondary"),
            MetricDefinition("time_to_research_seconds", "continuous", "secondary"),
            MetricDefinition("selected_persona_count", "continuous", "secondary"),
        ),
        guardrails=(Guardrail("research_request_success", 0.05),),
        minimum_sample_size=planned_sample_size_per_variant(),
        attribution_window_hours=1,
        stopping_rule="fixed-horizon",
        statistical_method="two-proportion-z-v1",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the DemandMap experiment.")
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Launch the immutable version after registration.",
    )
    args = parser.parse_args()

    args.registry.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    experiment = build_experiment()

    with ExperimentRegistry(args.registry) as registry:
        registered = registry.create(experiment)
        if args.launch:
            registered = registry.transition(EXPERIMENT_KEY, VERSION, Lifecycle.RUNNING)

    manifest = {
        "experiment": json.loads(serialize_experiment(experiment)),
        "lifecycle": registered.lifecycle.value,
        "planning": {
            "baseline_rate": BASELINE_RATE,
            "absolute_effect": ABSOLUTE_EFFECT,
            "alpha": ALPHA,
            "power": POWER,
            "required_per_variant": planned_sample_size_per_variant(),
            "required_total": 2 * planned_sample_size_per_variant(),
        },
    }
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
