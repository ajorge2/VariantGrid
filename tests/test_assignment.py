from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import unittest

from variantgrid.api import APIRequest, VariantGridAPI
from variantgrid.assignment import AssignmentEngine
from variantgrid.events import EventStore
from variantgrid.models import Experiment, VariantState
from variantgrid.registry import ExperimentRegistry, Lifecycle
from variantgrid.sdk import InProcessTransport, SDKConfig, VariantGridClient


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assignment_parity.json"


def weighted_experiment(
    *,
    key: str = "checkout_copy",
    version: int = 1,
    salt: str = "assignment-test",
    policy_version: str = "fixed-weight-v1",
    eligibility_rule: str = "all",
    weights: tuple[float, ...] = (0.5, 0.5),
) -> Experiment:
    return Experiment(
        key=key,
        version=version,
        states=tuple(
            VariantState(f"state-{index}", {"choice": index}, weight)
            for index, weight in enumerate(weights)
        ),
        primary_metric="converted",
        salt=salt,
        policy_version=policy_version,
        eligibility_rule=eligibility_rule,
    )


class AssignmentCorrectnessTests(unittest.TestCase):
    def test_repeat_assignment_is_fully_sticky(self) -> None:
        engine = AssignmentEngine(weighted_experiment())
        first = {f"subject-{index}": engine.assign(f"subject-{index}").state_id for index in range(10_000)}
        repeated = {subject: engine.assign(subject).state_id for subject in first}
        self.assertEqual(repeated, first)

    def test_version_salt_policy_and_experiment_namespaces_are_isolated(self) -> None:
        baseline = weighted_experiment()
        variants = (
            replace(baseline, version=2),
            replace(baseline, salt="changed-salt"),
            replace(baseline, policy_version="fixed-weight-v2"),
            replace(baseline, key="another_experiment"),
        )
        subjects = tuple(f"subject-{index}" for index in range(2_000))
        base_assignments = tuple(AssignmentEngine(baseline).assign(subject).state_id for subject in subjects)
        for changed in variants:
            with self.subTest(changed=changed):
                engine = AssignmentEngine(changed)
                assignments = tuple(engine.assign(subject).state_id for subject in subjects)
                self.assertNotEqual(assignments, base_assignments)
                self.assertEqual(assignments, tuple(engine.assign(subject).state_id for subject in subjects))

    def test_simultaneous_experiments_do_not_contaminate_each_other(self) -> None:
        first = AssignmentEngine(weighted_experiment(key="first"))
        second = AssignmentEngine(weighted_experiment(key="second"))
        before = tuple(first.assign(f"subject-{index}").state_id for index in range(1_000))
        tuple(second.assign(f"subject-{index}") for index in range(1_000))
        after = tuple(first.assign(f"subject-{index}").state_id for index in range(1_000))
        self.assertEqual(after, before)

    def test_one_and_many_state_experiments_are_supported(self) -> None:
        one = AssignmentEngine(weighted_experiment(weights=(1.0,)))
        self.assertEqual({one.assign(f"u-{index}").key for index in range(1_000)}, {"state-0"})
        many = AssignmentEngine(weighted_experiment(weights=tuple(float(index + 1) for index in range(64))))
        observed = {many.assign(f"u-{index}").key for index in range(20_000)}
        self.assertEqual(observed, {f"state-{index}" for index in range(64)})

    def test_metadata_explains_the_selected_weight_interval(self) -> None:
        result = AssignmentEngine(weighted_experiment(weights=(8.0, 2.0))).assign_with_metadata("u-1")
        self.assertTrue(result.eligible)
        self.assertEqual(result.assignment_probability, result.state.weight / 10.0)
        self.assertEqual(result.diagnostics.total_weight, 10.0)
        self.assertEqual(result.diagnostics.selected_weight, result.state.weight)
        self.assertLessEqual(result.diagnostics.interval_start, result.diagnostics.bucket)
        self.assertLess(result.diagnostics.bucket, result.diagnostics.interval_end)
        self.assertEqual(result.diagnostics.eligibility_source, "experiment_rule")

    def test_ineligibility_and_unknown_rules_fail_closed_with_immutable_defaults(self) -> None:
        disabled = AssignmentEngine(weighted_experiment(eligibility_rule="none"))
        fallback = disabled.assign_with_metadata("u-1", fallback_values={"choice": 0})
        self.assertFalse(fallback.eligible)
        self.assertEqual(fallback.reason, "subject_ineligible")
        self.assertEqual(fallback.fallback_values, {"choice": 0})
        with self.assertRaises(TypeError):
            fallback.fallback_values["choice"] = 1
        caller_cannot_override_disabled = disabled.assign_with_metadata("u-1", eligible=True)
        self.assertFalse(caller_cannot_override_disabled.eligible)
        self.assertEqual(
            caller_cannot_override_disabled.diagnostics.eligibility_source,
            "experiment_rule",
        )

        contextual = AssignmentEngine(weighted_experiment(eligibility_rule="country == 'US'"))
        unresolved = contextual.assign_with_metadata("u-1", fallback_values={"choice": 0})
        self.assertFalse(unresolved.eligible)
        self.assertEqual(unresolved.reason, "eligibility_context_required")
        explicitly_eligible = contextual.assign_with_metadata("u-1", eligible=True)
        self.assertTrue(explicitly_eligible.eligible)
        self.assertEqual(explicitly_eligible.diagnostics.eligibility_source, "caller")

    def test_unknown_assignment_implementations_fail_before_exposure(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported assignment algorithm"):
            AssignmentEngine(replace(weighted_experiment(), assignment_algorithm_version="sha512-v1"))
        with self.assertRaisesRegex(ValueError, "Unsupported assignment policy"):
            AssignmentEngine(replace(weighted_experiment(), policy_version="bandit-v1"))
        with self.assertRaisesRegex(ValueError, "Unsupported assignment policy"):
            AssignmentEngine(replace(weighted_experiment(), policy_version="fixed-weight-"))


class AssignmentParityFixtureTests(unittest.TestCase):
    def test_frozen_fixtures_match_engine_service_and_local_sdk(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text())
        for definition in fixture["experiments"]:
            experiment = Experiment(
                key=definition["key"],
                version=definition["version"],
                states=tuple(
                    VariantState(state["key"], state["values"], state["weight"])
                    for state in definition["states"]
                ),
                primary_metric="converted",
                salt=definition["salt"],
                assignment_algorithm_version=definition["algorithm_version"],
                policy_version=definition["policy_version"],
            )
            registry = ExperimentRegistry()
            registry.create(experiment)
            registry.transition(experiment.key, experiment.version, Lifecycle.RUNNING)
            store = EventStore()
            api = VariantGridAPI(registry, store)
            client = VariantGridClient(
                SDKConfig(experiment_versions={experiment.key: experiment.version}),
                InProcessTransport(api),
                local_experiments={(experiment.key, experiment.version): experiment},
            )
            try:
                for case in definition["cases"]:
                    with self.subTest(experiment=experiment.key, version=experiment.version, subject=case["subject_id"]):
                        direct = AssignmentEngine(experiment).assign_with_metadata(case["subject_id"])
                        service = api.handle(
                            APIRequest(
                                "POST",
                                "/v1/assign",
                                {
                                    "experiment_key": experiment.key,
                                    "experiment_version": experiment.version,
                                    "subject_id": case["subject_id"],
                                },
                            )
                        )
                        local = client.for_user(experiment.key, case["subject_id"])
                        self.assertEqual(direct.state.key, case["state_key"])
                        self.assertEqual(direct.state_id, case["state_id"])
                        self.assertEqual(direct.assignment_probability, case["assignment_probability"])
                        self.assertEqual(service.status, 200)
                        self.assertEqual(service.body["assignment"]["state_id"], case["state_id"])
                        self.assertEqual(service.body["assignment"]["state_key"], case["state_key"])
                        self.assertEqual(
                            service.body["assignment"]["diagnostics"]["bucket"],
                            direct.diagnostics.bucket,
                        )
                        self.assertEqual(local.debug_metadata.state_id, case["state_id"])
                        self.assertEqual(local.debug_metadata.state_key, case["state_key"])
                        self.assertEqual(local.debug_metadata.policy_version, definition["policy_version"])
                        self.assertEqual(local.debug_metadata.bucket, direct.diagnostics.bucket)
                        self.assertEqual(
                            local.debug_metadata.eligibility_source,
                            direct.diagnostics.eligibility_source,
                        )
            finally:
                client.close()
                store.close()
                registry.close()


if __name__ == "__main__":
    unittest.main()
