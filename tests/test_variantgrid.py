from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from variantgrid import (
    AssignmentEngine,
    Event,
    EventStore,
    Experiment,
    ExperimentRegistry,
    Factor,
    Lifecycle,
    Rule,
    VariableType,
    VariantState,
    analyze_continuous_metric,
    evaluate_binary_decision,
    minimum_detectable_effect,
    stable_state_id,
)


def sample_experiment(version: int = 1) -> Experiment:
    return Experiment.from_factors(
        key="onboarding",
        version=version,
        factors=(Factor("headline", ("control", "benefit")), Factor("cta", ("start", "try"))),
        rules=(Rule({"headline": "control"}, {"cta": "start"}),),
        primary_metric="activated",
        salt="variantgrid-test",
    )


class VariantGridTests(unittest.TestCase):
    def test_constraints_remove_invalid_states(self) -> None:
        experiment = sample_experiment()
        self.assertEqual(len(experiment.states), 3)
        self.assertNotIn("headline=control|cta=try", {state.key for state in experiment.states})

    def test_assignment_is_sticky(self) -> None:
        engine = AssignmentEngine(sample_experiment())
        first = engine.assign("user-42")
        self.assertEqual(first, engine.assign("user-42"))

    def test_versions_are_isolated(self) -> None:
        first = AssignmentEngine(sample_experiment(1)).assign("user-42")
        second = AssignmentEngine(sample_experiment(2)).assign("user-42")
        self.assertNotEqual(
            (sample_experiment(1).version, first.key),
            (sample_experiment(2).version, second.key),
        )

    def test_duplicate_events_are_idempotent(self) -> None:
        store = EventStore()
        event = Event("e1", "onboarding", 1, "u1", "exposure", "control")
        self.assertTrue(store.ingest(event))
        self.assertFalse(store.ingest(event))
        self.assertEqual(store.event_count(), 1)

    def test_goal_before_exposure_is_still_attributed(self) -> None:
        store = EventStore()
        exposure_time = datetime.now(timezone.utc)
        goal_time = exposure_time + timedelta(minutes=5)
        # Receipt order is reversed, while event time still preserves causality.
        store.ingest(
            Event("g1", "onboarding", 1, "u1", "goal", metric="activated", value=1,
                  occurred_at=goal_time.isoformat())
        )
        store.ingest(
            Event("x1", "onboarding", 1, "u1", "exposure", "treatment",
                  occurred_at=exposure_time.isoformat())
        )
        self.assertEqual(store.binary_counts("onboarding", 1, "activated"), {"treatment": (1, 1)})

    def test_missing_exposure_is_not_attributed(self) -> None:
        store = EventStore()
        store.ingest(Event("g1", "onboarding", 1, "u1", "goal", metric="activated", value=1))
        self.assertEqual(store.binary_counts("onboarding", 1, "activated"), {})

    def test_late_event_uses_experiment_time_not_receipt_order(self) -> None:
        store = EventStore()
        late = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        later = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        store.ingest(Event("x1", "onboarding", 1, "u1", "exposure", "control", occurred_at=late))
        store.ingest(Event("g1", "onboarding", 1, "u1", "goal", metric="activated", value=1, occurred_at=later))
        self.assertEqual(store.binary_counts("onboarding", 1, "activated"), {"control": (1, 1)})

    def test_readouts_do_not_cross_versions(self) -> None:
        store = EventStore()
        store.ingest(Event("x1", "onboarding", 1, "u1", "exposure", "control"))
        store.ingest(Event("g2", "onboarding", 2, "u1", "goal", metric="activated", value=1))
        self.assertEqual(store.binary_counts("onboarding", 1, "activated"), {"control": (1, 0)})

    def test_typed_factors_reject_mismatched_values(self) -> None:
        Factor("steps", (1, 2), VariableType.INTEGER)
        with self.assertRaises(ValueError):
            Factor("steps", (1, "two"), VariableType.INTEGER)

    def test_state_identity_is_independent_of_mapping_order(self) -> None:
        self.assertEqual(stable_state_id({"a": 1, "b": 2}), stable_state_id({"b": 2, "a": 1}))

    def test_registry_replays_and_freezes_launched_versions(self) -> None:
        registry = ExperimentRegistry()
        original = sample_experiment()
        created = registry.create(original)
        self.assertEqual(created.experiment.states, original.states)
        running = registry.transition("onboarding", 1, Lifecycle.RUNNING)
        self.assertEqual(running.lifecycle, Lifecycle.RUNNING)
        with self.assertRaises(ValueError):
            registry.replace_draft(replace(original, primary_metric="retained"))
        clone = registry.clone_version("onboarding", 1, 2)
        self.assertEqual(clone.lifecycle, Lifecycle.DRAFT)
        self.assertEqual(
            [state.state_id for state in clone.experiment.states],
            [state.state_id for state in original.states],
        )

    def test_invalid_registry_transition_is_rejected(self) -> None:
        registry = ExperimentRegistry()
        registry.create(sample_experiment())
        with self.assertRaises(ValueError):
            registry.transition("onboarding", 1, Lifecycle.STOPPED)

    def test_assignment_metadata_and_fallback_are_explicit(self) -> None:
        engine = AssignmentEngine(sample_experiment())
        result = engine.assign_with_metadata("u1")
        self.assertTrue(result.eligible)
        self.assertIsNotNone(result.state_id)
        self.assertGreater(result.assignment_probability, 0)
        fallback = engine.assign_with_metadata("u2", eligible=False, fallback_values={"cta": "start"})
        self.assertFalse(fallback.eligible)
        self.assertEqual(fallback.reason, "subject_ineligible")
        self.assertEqual(fallback.fallback_values, {"cta": "start"})

    def test_policy_versions_are_assignment_isolated(self) -> None:
        original = sample_experiment()
        changed = replace(original, policy_version="fixed-weight-v2")
        first = AssignmentEngine(original).assign_with_metadata("user-42")
        second = AssignmentEngine(changed).assign_with_metadata("user-42")
        self.assertNotEqual(first.policy_version, second.policy_version)

    def test_concurrent_duplicates_are_idempotent(self) -> None:
        store = EventStore()
        event = Event("same", "onboarding", 1, "u1", "exposure", "control")
        with ThreadPoolExecutor(max_workers=8) as pool:
            inserted = list(pool.map(store.ingest, [event] * 32))
        self.assertEqual(sum(inserted), 1)
        self.assertEqual(store.event_count(), 1)

    def test_outcome_before_exposure_time_is_not_attributed(self) -> None:
        store = EventStore()
        now = datetime.now(timezone.utc)
        store.ingest(Event("g1", "onboarding", 1, "u1", "goal", metric="activated", value=1,
                           occurred_at=now.isoformat()))
        store.ingest(Event("x1", "onboarding", 1, "u1", "exposure", "control",
                           occurred_at=(now + timedelta(minutes=1)).isoformat()))
        self.assertEqual(store.binary_counts("onboarding", 1, "activated"), {"control": (1, 0)})

    def test_attribution_window_is_enforced(self) -> None:
        store = EventStore()
        now = datetime.now(timezone.utc)
        store.ingest(Event("x1", "onboarding", 1, "u1", "exposure", "control",
                           occurred_at=now.isoformat()))
        store.ingest(Event("g1", "onboarding", 1, "u1", "goal", metric="activated", value=1,
                           occurred_at=(now + timedelta(hours=25)).isoformat()))
        self.assertEqual(
            store.binary_counts("onboarding", 1, "activated", attribution_window_hours=24),
            {"control": (1, 0)},
        )

    def test_continuous_readout_and_mde(self) -> None:
        readout = analyze_continuous_metric(
            control_values=(1, 2, 3, 4), treatment_values=(2, 3, 4, 5)
        )
        self.assertAlmostEqual(readout.absolute_lift, 1.0)
        self.assertGreater(minimum_detectable_effect(baseline_rate=0.10, subjects_per_variant=1000), 0)

    def test_integrity_failure_suppresses_recommendation(self) -> None:
        decision = evaluate_binary_decision(
            control_exposed=100,
            control_converted=10,
            treatment_exposed=100,
            treatment_converted=20,
            minimum_sample_size_per_variant=1000,
            guardrail_breaches=("crash_rate",),
        )
        self.assertFalse(decision.eligible_for_decision)
        self.assertIsNone(decision.recommendation)
        self.assertIn("insufficient_sample_size", decision.integrity_failures)
        self.assertIn("guardrail_breach:crash_rate", decision.integrity_failures)


if __name__ == "__main__":
    unittest.main()
