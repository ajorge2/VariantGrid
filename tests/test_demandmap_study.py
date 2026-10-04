from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

from variantgrid.assignment import AssignmentEngine
from variantgrid.registry import ExperimentRegistry, Lifecycle


STUDY_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "studies"
    / "demandmap-guided-calibration"
    / "register_experiment.py"
)
SPEC = importlib.util.spec_from_file_location("demandmap_study", STUDY_SCRIPT)
assert SPEC and SPEC.loader
study = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(study)


class DemandMapStudyTest(unittest.TestCase):
    def test_preregistered_configuration_is_replayable_and_sticky(self) -> None:
        experiment = study.build_experiment()
        self.assertEqual(study.planned_sample_size_per_variant(), 59)
        self.assertEqual(experiment.minimum_sample_size, 59)
        self.assertEqual(
            {state.values["calibration_mode"] for state in experiment.states},
            {"self_serve", "guided"},
        )
        self.assertEqual(experiment.replay_states(), experiment.states)

        engine = AssignmentEngine(experiment)
        first = engine.assign("participant-001")
        repeat = engine.assign("participant-001")
        self.assertEqual(first.state_id, repeat.state_id)

    def test_registry_keeps_launched_study_immutable(self) -> None:
        experiment = study.build_experiment()
        with tempfile.TemporaryDirectory() as directory:
            with ExperimentRegistry(Path(directory) / "study.sqlite") as registry:
                registry.create(experiment)
                launched = registry.transition(
                    experiment.key,
                    experiment.version,
                    Lifecycle.RUNNING,
                )
                self.assertEqual(launched.lifecycle, Lifecycle.RUNNING)
                with self.assertRaises(ValueError):
                    registry.create(experiment)


if __name__ == "__main__":
    unittest.main()
