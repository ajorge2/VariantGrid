from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from variantgrid.assignment import AssignmentEngine
from variantgrid.models import (
    Experiment,
    Factor,
    Guardrail,
    MetricDefinition,
    Rule,
    VariableType,
    generate_state_space,
    stable_state_id,
)
from variantgrid.registry import ExperimentRegistry, Lifecycle, serialize_experiment
from variantgrid.registry_migrations import migrate_registry, rollback_registry


def complete_experiment(version: int = 1) -> Experiment:
    return Experiment.from_factors(
        key="registry_fixture",
        version=version,
        hypothesis="A guided pro setup improves activation without harming reliability.",
        factors=(
            Factor("plan", ("free", "pro"), VariableType.ENUM),
            Factor("steps", (2, 4), VariableType.INTEGER),
            Factor("intensity", (0.5, 1.0), VariableType.FLOAT),
            Factor("guided", (False, True), VariableType.BOOLEAN),
            Factor(
                "pro_theme",
                ("blue", "green"),
                VariableType.ENUM,
                active_when={"plan": "pro"},
            ),
        ),
        rules=(
            Rule(
                {"plan": "pro", "pro_theme": "blue"},
                {"guided": True},
                "Blue pro onboarding requires the guided setup.",
            ),
        ),
        primary_metric="activated",
        metrics=(
            MetricDefinition("activated", "binary", "primary"),
            MetricDefinition("time_to_value", "continuous", "secondary"),
        ),
        guardrails=(Guardrail("crash_rate", 0.01),),
        salt="registry-fixture",
        minimum_sample_size=1000,
        eligibility_rule="country == 'US'",
        attribution_window_hours=72,
        stopping_rule="fixed horizon: 14 days",
        statistical_method="two-proportion-z-v1",
    )


class StateGenerationTests(unittest.TestCase):
    def test_typed_conditional_variables_and_rejection_reasons_are_explicit(self) -> None:
        experiment = complete_experiment()
        preview = experiment.preview_states()

        self.assertTrue(preview.accepted)
        self.assertTrue(preview.rejected)
        free_states = [state for state in preview.accepted if state.values["plan"] == "free"]
        self.assertTrue(free_states)
        self.assertTrue(all("pro_theme" not in state.values for state in free_states))
        self.assertEqual(
            {rejected.reasons for rejected in preview.rejected},
            {("Blue pro onboarding requires the guided setup.",)},
        )

    def test_conditional_variables_reject_forward_or_unknown_dependencies(self) -> None:
        with self.assertRaisesRegex(ValueError, "earlier factor"):
            generate_state_space(
                (
                    Factor("child", ("on",), active_when={"parent": "yes"}),
                    Factor("parent", ("yes", "no")),
                )
            )

    def test_canonical_ids_ignore_mapping_insertion_order(self) -> None:
        self.assertEqual(
            stable_state_id({"plan": "pro", "guided": True}),
            stable_state_id({"guided": True, "plan": "pro"}),
        )


class RegistryPersistenceTests(unittest.TestCase):
    def test_file_round_trip_replays_identical_order_ids_and_assignment_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.sqlite3"
            original = complete_experiment()
            with ExperimentRegistry(path) as registry:
                created = registry.create(original)
                registry.transition(original.key, original.version, Lifecycle.RUNNING)
                original_json = created.configuration_json
                original_ids = tuple(state.state_id for state in original.states)
                original_assignments = tuple(
                    AssignmentEngine(original).assign(f"subject-{index}").state_id
                    for index in range(1000)
                )

            with ExperimentRegistry(path) as reopened:
                stored = reopened.get(original.key, original.version)
                replayed = reopened.replay(original.key, original.version)
                self.assertEqual(stored.configuration_json, original_json)
                self.assertEqual(tuple(state.state_id for state in replayed.states), original_ids)
                self.assertEqual(
                    tuple(
                        AssignmentEngine(replayed).assign(f"subject-{index}").state_id
                        for index in range(1000)
                    ),
                    original_assignments,
                )

    def test_metrics_guardrails_and_immutable_decisions_are_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.sqlite3"
            with ExperimentRegistry(path) as registry:
                experiment = complete_experiment()
                registry.create(experiment)
                registry.transition(experiment.key, 1, Lifecycle.RUNNING)
                recorded = registry.record_decision(
                    experiment.key,
                    1,
                    "continue",
                    {"readout_sha256": "fixture", "integrity": "passed"},
                )
                self.assertEqual(recorded.decision, "continue")

            with ExperimentRegistry(path) as reopened:
                self.assertEqual(
                    {metric.name for metric in reopened.metrics("registry_fixture", 1)},
                    {"activated", "time_to_value"},
                )
                self.assertEqual(reopened.guardrails("registry_fixture", 1)[0].metric, "crash_rate")
                self.assertEqual(reopened.decisions("registry_fixture", 1)[0].evidence["integrity"], "passed")
                with self.assertRaises(sqlite3.IntegrityError):
                    reopened.connection.execute("UPDATE decisions SET decision = 'ship'")

    def test_launched_version_is_immutable_even_through_direct_sql(self) -> None:
        registry = ExperimentRegistry()
        self.addCleanup(registry.close)
        experiment = complete_experiment()
        registry.create(experiment)
        registry.transition(experiment.key, 1, Lifecycle.RUNNING)
        with self.assertRaisesRegex(ValueError, "immutable"):
            registry.replace_draft(replace(experiment, salt="mutated"))
        with self.assertRaises(sqlite3.IntegrityError):
            registry.connection.execute(
                "UPDATE experiment_versions SET configuration_json = '{}' "
                "WHERE experiment_key = ? AND version = 1",
                (experiment.key,),
            )

    def test_material_edit_clones_to_new_draft_and_preserves_source(self) -> None:
        registry = ExperimentRegistry()
        self.addCleanup(registry.close)
        experiment = complete_experiment()
        source = registry.create(experiment)
        registry.transition(experiment.key, 1, Lifecycle.RUNNING)
        clone = registry.clone_with_edits(experiment.key, 1, salt="registry-fixture-v2")

        self.assertEqual(clone.experiment.version, 2)
        self.assertEqual(clone.lifecycle, Lifecycle.DRAFT)
        self.assertEqual(clone.experiment.salt, "registry-fixture-v2")
        self.assertEqual(registry.get(experiment.key, 1).configuration_json, source.configuration_json)

    def test_invalid_lifecycle_transitions_fail_visibly(self) -> None:
        registry = ExperimentRegistry()
        self.addCleanup(registry.close)
        registry.create(complete_experiment())
        with self.assertRaisesRegex(ValueError, "draft -> stopped"):
            registry.transition("registry_fixture", 1, Lifecycle.STOPPED)
        registry.transition("registry_fixture", 1, Lifecycle.RUNNING)
        registry.transition("registry_fixture", 1, Lifecycle.STOPPED)
        with self.assertRaisesRegex(ValueError, "stopped -> running"):
            registry.transition("registry_fixture", 1, Lifecycle.RUNNING)

    def test_checksum_detects_storage_tampering(self) -> None:
        registry = ExperimentRegistry()
        self.addCleanup(registry.close)
        registry.create(complete_experiment())
        registry.connection.execute(
            "UPDATE experiment_versions SET configuration_json = '{}' "
            "WHERE experiment_key = 'registry_fixture' AND version = 1"
        )
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            registry.get("registry_fixture", 1)


class RegistryMigrationTests(unittest.TestCase):
    def test_v1_database_upgrades_rolls_back_and_reapplies_without_losing_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.sqlite3"
            connection = sqlite3.connect(path)
            migrate_registry(connection, target=1)
            experiment = complete_experiment()
            payload = serialize_experiment(experiment)
            connection.execute(
                "INSERT INTO experiment_versions VALUES (?, ?, ?, ?)",
                (experiment.key, 1, Lifecycle.RUNNING.value, payload),
            )
            connection.commit()
            connection.close()

            with ExperimentRegistry(path) as upgraded:
                self.assertEqual(upgraded.schema_version, 2)
                self.assertEqual(upgraded.get(experiment.key, 1).configuration_json, payload)
                self.assertEqual(rollback_registry(upgraded.connection), 1)
                self.assertEqual(migrate_registry(upgraded.connection), 2)

            with ExperimentRegistry(path) as reopened:
                self.assertEqual(reopened.replay(experiment.key, 1), experiment)


if __name__ == "__main__":
    unittest.main()
