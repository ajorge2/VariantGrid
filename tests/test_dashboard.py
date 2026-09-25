import json
import unittest
from dataclasses import replace

from variantgrid.dashboard import (
    ConstraintDefinition,
    DashboardLifecycle,
    OperatorSandbox,
    default_draft,
    preview_states,
    render_dashboard,
    validate_draft,
)


class DashboardWorkflowTests(unittest.TestCase):
    def test_preview_preserves_valid_and_rejected_states_with_reason(self) -> None:
        preview = preview_states(default_draft())

        self.assertEqual(3, len(preview.valid))
        self.assertEqual(1, len(preview.rejected))
        self.assertEqual(
            ("Blank starts cannot show the template-guided tour.",),
            preview.rejected[0].reasons,
        )

    def test_required_decision_inputs_block_launch(self) -> None:
        cases = (
            replace(default_draft(), hypothesis=""),
            replace(default_draft(), primary_metric=""),
            replace(default_draft(), guardrail_maximum_regression=None),
        )

        for draft in cases:
            with self.subTest(draft=draft):
                sandbox = OperatorSandbox(draft)
                with self.assertRaisesRegex(ValueError, "required"):
                    sandbox.launch(review_confirmed=True)

    def test_empty_valid_state_space_blocks_launch(self) -> None:
        impossible = ConstraintDefinition(
            when={}, require={"starter_type": "never-a-valid-value"}, reason="Fixture rejects all states."
        )
        sandbox = OperatorSandbox(replace(default_draft(), constraints=(impossible,)))

        self.assertIn("Constraints eliminated every possible state.", validate_draft(sandbox.draft))
        with self.assertRaisesRegex(ValueError, "eliminated every possible state"):
            sandbox.launch(review_confirmed=True)

    def test_launch_requires_explicit_review_and_freezes_configuration(self) -> None:
        sandbox = OperatorSandbox()
        with self.assertRaisesRegex(ValueError, "immutable launch review"):
            sandbox.launch(review_confirmed=False)

        launched = sandbox.launch(review_confirmed=True)
        original_configuration = launched.configuration_json
        frozen = json.loads(original_configuration)
        self.assertEqual([0.5, 0.25, 0.25], [state["weight"] for state in frozen["states"]])
        with self.assertRaisesRegex(ValueError, "immutable"):
            sandbox.update_draft(replace(sandbox.draft, hypothesis="A changed hypothesis"))

        sandbox.clone_current()
        sandbox.update_draft(replace(sandbox.draft, hypothesis="A changed hypothesis"))
        self.assertEqual(original_configuration, sandbox.versions[1].configuration_json)
        self.assertNotIn("changed hypothesis", original_configuration)

    def test_health_is_rendered_before_effect_and_every_chart_is_labeled(self) -> None:
        sandbox = OperatorSandbox()
        sandbox.launch(review_confirmed=True)
        page = render_dashboard(sandbox)

        self.assertLess(page.index('id="data-health"'), page.index('id="readout"'))
        self.assertIn("n=500", page)
        self.assertIn("unit: activation rate", page)
        self.assertIn("2026-09-01 to 2026-09-14 UTC", page)
        self.assertIn("version: 1", page)
        self.assertIn("95% CI", page)

    def test_integrity_failures_withhold_effects_and_block_decisions(self) -> None:
        fixtures = (
            "srm",
            "missing_exposure",
            "ingestion_delay",
            "guardrail_breach",
            "version_disagreement",
            "stale_configuration",
            "underpowered",
        )

        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                sandbox = OperatorSandbox()
                sandbox.launch(review_confirmed=True)
                sandbox.inject_failure(fixture)
                page = render_dashboard(sandbox)
                self.assertIn("Effect estimates withheld", page)
                self.assertNotIn("Recommendation: <strong>", page)
                with self.assertRaisesRegex(ValueError, "Decision blocked"):
                    sandbox.record_decision("ship")

    def test_decision_preserves_full_immutable_evidence_snapshot(self) -> None:
        sandbox = OperatorSandbox()
        sandbox.launch(review_confirmed=True)

        snapshot = sandbox.record_decision("ship")
        evidence = json.loads(snapshot.evidence_json)

        self.assertEqual(1, snapshot.version)
        self.assertEqual("ship", evidence["operator_decision"])
        self.assertEqual(1, evidence["experiment_version"])
        self.assertEqual(
            sandbox.versions[1].configuration_json,
            evidence["immutable_configuration_json"],
        )
        self.assertIn("confidence_interval_95", evidence["readout"])
        self.assertEqual("treatment", evidence["recommendation"])

    def test_lifecycle_controls_preserve_history_and_rollback(self) -> None:
        sandbox = OperatorSandbox()
        first = sandbox.launch(review_confirmed=True)
        first_configuration = first.configuration_json
        sandbox.transition("pause")
        self.assertEqual(DashboardLifecycle.PAUSED, sandbox.current_version.lifecycle)
        sandbox.transition("resume")
        sandbox.clone_current()
        sandbox.update_draft(replace(sandbox.draft, target_absolute_effect=0.07))
        sandbox.launch(review_confirmed=True)

        self.assertEqual(2, sandbox.deployed_version)
        sandbox.rollback()
        self.assertEqual(1, sandbox.deployed_version)
        self.assertEqual(first_configuration, sandbox.versions[1].configuration_json)
        self.assertEqual(2, len(sandbox.versions))

    def test_pause_stop_and_kill_are_explicit_irreversible_transitions(self) -> None:
        for action, expected in (
            ("stop", DashboardLifecycle.STOPPED),
            ("kill", DashboardLifecycle.KILLED),
        ):
            with self.subTest(action=action):
                sandbox = OperatorSandbox()
                sandbox.launch(review_confirmed=True)
                configuration = sandbox.current_version.configuration_json
                sandbox.transition(action)
                self.assertEqual(expected, sandbox.current_version.lifecycle)
                self.assertEqual(configuration, sandbox.current_version.configuration_json)
                with self.assertRaises(ValueError):
                    sandbox.transition("resume")


if __name__ == "__main__":
    unittest.main()
