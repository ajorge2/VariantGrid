import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from variantgrid.dashboard import (
    ConstraintDefinition,
    DashboardLifecycle,
    OperatorSandbox,
    default_draft,
    load_public_claims,
    preview_states,
    render_dashboard,
    validate_draft,
)
from evidence_fixtures import write_approved_evidence


class DashboardWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.project_root = write_approved_evidence(Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def render(self, sandbox: OperatorSandbox) -> str:
        return render_dashboard(sandbox, self.project_root)

    def test_preview_preserves_valid_and_rejected_states_with_reason(self) -> None:
        preview = preview_states(default_draft())

        self.assertEqual(3, len(preview.valid))
        self.assertEqual(1, len(preview.rejected))
        self.assertEqual(
            ("Blank starts cannot show the template-guided tour.",),
            preview.rejected[0].reasons,
        )

    def test_rule_editor_excludes_combinations_and_blocks_contradictory_sequences(self) -> None:
        exclusion = ConstraintDefinition(
            when={"starter_type": "blank", "guided_tour": True},
            require={},
            reason="Blank then guided is not a valid product sequence.",
            effect="exclude",
            match_order="in_order",
            sequence=("starter_type", "guided_tour"),
        )
        draft = replace(default_draft(), constraints=(exclusion,))
        preview = preview_states(draft)

        self.assertEqual(3, len(preview.valid))
        self.assertEqual(1, len(preview.rejected))
        self.assertEqual((), validate_draft(draft))
        sandbox = OperatorSandbox(draft)
        self.addCleanup(sandbox.event_store.close)
        launched = sandbox.launch(review_confirmed=True)
        frozen = json.loads(launched.configuration_json)
        self.assertEqual("exclude", frozen["rules"][0]["effect"])
        self.assertEqual("in_order", frozen["rules"][0]["match_order"])
        self.assertEqual(["starter_type", "guided_tour"], frozen["rules"][0]["sequence"])
        self.assertEqual(3, len(sandbox.live_readout()["states"]))

        contradictory = replace(
            draft,
            constraints=(
                ConstraintDefinition(
                    when={"guided_tour": True, "starter_type": "blank"},
                    require={},
                    reason="The declared sequence runs backward.",
                    effect="exclude",
                    match_order="in_order",
                    sequence=("guided_tour", "starter_type"),
                ),
            ),
        )
        self.assertTrue(
            any("contradicts factor declaration order" in error for error in validate_draft(contradictory))
        )
        with self.assertRaisesRegex(ValueError, "contradicts factor declaration order"):
            OperatorSandbox(contradictory).launch(review_confirmed=True)

    def test_sdk_events_drive_live_dashboard_readout(self) -> None:
        sandbox = OperatorSandbox()
        self.addCleanup(sandbox.event_store.close)
        sandbox.launch(review_confirmed=True)

        viewed = sandbox.run_reference_product("live-user-17")
        converted = sandbox.run_reference_product("live-user-17", convert=True)
        readout = sandbox.live_readout()

        self.assertEqual(viewed["state_key"], converted["state_key"])
        self.assertEqual("live", readout["status"])
        self.assertEqual(1, readout["event_totals"]["assignment"])
        self.assertEqual(2, readout["event_totals"]["exposure"])
        self.assertEqual(1, readout["event_totals"]["goal"])
        assigned_state = next(
            state for state in readout["states"] if state["state_key"] == viewed["state_key"]
        )
        self.assertEqual(1, assigned_state["assigned"])
        self.assertEqual(1, assigned_state["exposed"])
        self.assertEqual(1, assigned_state["converted"])
        page = self.render(sandbox)
        self.assertIn("Live SDK telemetry", page)
        self.assertIn("window.setInterval(refresh, 2000)", page)
        self.assertIn("Open instrumented product", page)

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
        page = self.render(sandbox)

        self.assertLess(page.index('id="data-health"'), page.index('id="readout"'))
        self.assertIn("n=500", page)
        self.assertIn("unit: activation rate", page)
        self.assertIn("2026-09-01 to 2026-09-14 UTC", page)
        self.assertIn("version: 1", page)
        self.assertIn("95% CI", page)

    def test_minimal_shell_progressively_discloses_dense_controls(self) -> None:
        page = self.render(OperatorSandbox())

        self.assertIn("Trust an experiment before acting on it.", page)
        self.assertEqual(4, page.count("<section"))
        self.assertIn('<details id="configuration" class="workflow-step" open>', page)
        self.assertIn('<details id="state-preview" class="workflow-step">', page)
        self.assertIn('<details id="data-health" class="workflow-step">', page)
        self.assertIn("Advanced experiment definition", page)
        self.assertNotIn("Open a claim only when you want its proof.", page)
        self.assertNotIn("Move through the lifecycle one step at a time.", page)
        self.assertNotIn("View proof", page)
        self.assertNotIn("Configuration complete", page)
        self.assertNotIn("Try a user ID", page)
        self.assertNotIn("not launched · local demo", page)
        self.assertNotIn("· approved", page.lower())

    def test_viewport_layout_preserves_sticky_navigation_and_mobile_reflow(self) -> None:
        page = self.render(OperatorSandbox())

        self.assertIn('</header><div class="nav-shell"><nav class="section-nav"', page)
        self.assertIn(".nav-shell{padding:0 24px;position:sticky;top:0", page)
        self.assertIn("section{border-top:1px solid var(--ink);", page)
        self.assertIn("scroll-margin-top:66px", page)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", page)
        self.assertIn("overflow-wrap:anywhere", page)

    def test_public_proof_surface_uses_only_approved_generated_claims(self) -> None:
        claims = load_public_claims(self.project_root)
        self.assertEqual(("VG-1", "VG-2R", "VG-3"), tuple(claim["claim_id"] for claim in claims))
        self.assertTrue(all(claim["status"] == "approved" for claim in claims))

        page = self.render(OperatorSandbox())

        self.assertIn('id="assignment-lab"', page)
        self.assertIn('id="event-integrity-lab"', page)
        self.assertIn('id="validation-evidence"', page)
        self.assertIn("System snapshot", page)
        self.assertIn("System validation results", page)
        self.assertIn("Experiment workflow", page)
        self.assertIn("Assignment consistency", page)
        self.assertIn("Inference and event integrity", page)
        self.assertIn("3M simulated assignments", page)
        self.assertIn("1.417 µs median sampled p95 across five local trials", page)
        self.assertIn("VG-1.json", page)
        self.assertIn("VG-2R.json", page)
        self.assertIn("VG-3.json", page)
        self.assertIn("href='#assignment-lab'", page)
        self.assertIn("href='#event-integrity-lab'", page)
        self.assertIn("1,000,000 assignments each", page)
        self.assertIn("0.0402 pp max drift", page)
        self.assertIn("5.13% false positives", page)
        self.assertIn("94.87% interval coverage", page)
        self.assertNotIn("0.06 percentage-point allocation drift", page)
        self.assertNotIn("1.46 microsecond", page)
        self.assertIn("PYTHONPATH=src python3 scripts/verify_release.py", page)
        self.assertIn("CPU model not reported", page)
        self.assertIn("do not establish production durability", page)
        self.assertNotIn("Resume claim proof map", page)
        self.assertNotIn(">Claims<", page)
        self.assertNotIn(">Proof<", page)
        self.assertNotIn("Built experimentation infrastructure", page)
        self.assertNotIn("Engineered deterministic", page)
        self.assertNotIn("Built a Monte Carlo", page)

    def test_public_claims_fail_closed_when_release_provenance_disagrees(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            claims_directory = root / "artifacts" / "claims"
            claims_directory.mkdir(parents=True)
            (root / "artifacts" / "release_manifest.json").write_text(
                json.dumps(
                    {
                        "claims": {"VG-1": "approved", "VG-2R": "approved", "VG-3": "approved"},
                        "source_fingerprint_sha256": "release-fingerprint",
                    }
                ),
                encoding="utf-8",
            )
            for claim_id in ("VG-1", "VG-2R", "VG-3"):
                (claims_directory / f"{claim_id}.json").write_text(
                    json.dumps(
                        {
                            "claim_id": claim_id,
                            "status": "approved",
                            "source_fingerprint_sha256": "different-fingerprint",
                        }
                    ),
                    encoding="utf-8",
                )

            claims = load_public_claims(root)

        self.assertTrue(all(claim["status"] == "unavailable" for claim in claims))
        self.assertTrue(all("do not agree" in claim["error"] for claim in claims))

    def test_assignment_lab_proves_repeat_assignment_and_version_namespace(self) -> None:
        sandbox = OperatorSandbox()
        sandbox.assignment_subject = "auditable-subject-17"

        page = self.render(sandbox)

        self.assertIn("auditable-subject-17", page)
        self.assertIn("PASS · repeat assignment identical", page)
        self.assertIn("PASS · version namespace isolated", page)
        self.assertIn("sha256-v1", page)
        self.assertIn("fixed-weight-v1", page)
        self.assertIn("landing in the same state is valid", page)

    def test_event_integrity_lab_runs_all_protected_scenarios(self) -> None:
        page = self.render(OperatorSandbox())

        self.assertIn("5/5 passed", page)
        for label in ("Duplicate", "Missing Exposure", "Late", "Reordered", "Cross Version"):
            with self.subTest(label=label):
                self.assertIn(f"<strong>{label}</strong>", page)
        self.assertIn("Assignment does not count as exposure", page)
        self.assertIn("cross-version contamination must produce zero conversions", page)
        self.assertIn("not distributed delivery", page)

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
                page = self.render(sandbox)
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
