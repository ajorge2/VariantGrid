from __future__ import annotations

import json
import math
import random
import unittest

from variantgrid.analysis import (
    AnalysisPlan,
    GuardrailRule,
    StatisticalMethodMetadata,
    analyze_binary_metric,
    analyze_continuous_metric,
    binary_test_power,
    continuous_minimum_detectable_effect,
    continuous_test_power,
    evaluate_binary_decision,
    evaluate_continuous_decision,
    evaluate_guardrail,
    inference_warnings,
    minimum_detectable_effect,
    required_sample_size,
    summarize_effect_recovery,
)


class MetadataAndValidationTests(unittest.TestCase):
    def test_method_metadata_is_canonical_and_round_trips(self) -> None:
        metadata = analyze_binary_metric(
            control_exposed=1_000,
            control_converted=100,
            treatment_exposed=1_000,
            treatment_converted=120,
            alpha=0.01,
        ).method_metadata
        self.assertIsNotNone(metadata)
        assert metadata is not None
        payload = metadata.to_json()
        self.assertEqual(metadata, StatisticalMethodMetadata.from_json(payload))
        self.assertEqual(payload, metadata.to_json())
        self.assertEqual("two-proportion-z-v1", metadata.method_id)
        self.assertEqual(0.01, metadata.alpha)
        readout = analyze_binary_metric(
            control_exposed=1_000,
            control_converted=100,
            treatment_exposed=1_000,
            treatment_converted=120,
        )
        self.assertIsNotNone(readout.relative_confidence_interval_95)

    def test_analysis_plan_is_persistable_and_rejects_invalid_inputs(self) -> None:
        plan = AnalysisPlan(
            primary_metric="activated",
            metric_type="binary",
            minimum_sample_size_per_variant=2_000,
            planned_looks=1,
        )
        self.assertEqual(plan, AnalysisPlan.from_json(plan.to_json()))
        self.assertEqual("activated", json.loads(plan.to_json())["primary_metric"])
        with self.assertRaises(ValueError):
            AnalysisPlan(primary_metric="", metric_type="binary")

    def test_binary_counts_must_be_logically_valid(self) -> None:
        with self.assertRaises(ValueError):
            analyze_binary_metric(
                control_exposed=10,
                control_converted=11,
                treatment_exposed=10,
                treatment_converted=1,
            )

    def test_continuous_values_must_be_finite(self) -> None:
        with self.assertRaises(ValueError):
            analyze_continuous_metric(
                control_values=(1.0, math.nan), treatment_values=(1.0, 2.0)
            )


class PlanningTests(unittest.TestCase):
    def test_binary_mde_and_required_sample_size_invert_power(self) -> None:
        sample_size = required_sample_size(
            baseline_rate=0.10, absolute_effect=0.02, alpha=0.05, power=0.80
        )
        self.assertGreaterEqual(
            binary_test_power(
                baseline_rate=0.10,
                absolute_effect=0.02,
                subjects_per_variant=sample_size,
            ),
            0.80,
        )
        if sample_size > 1:
            self.assertLess(
                binary_test_power(
                    baseline_rate=0.10,
                    absolute_effect=0.02,
                    subjects_per_variant=sample_size - 1,
                ),
                0.80,
            )
        mde = minimum_detectable_effect(
            baseline_rate=0.10, subjects_per_variant=sample_size, power=0.80
        )
        self.assertAlmostEqual(0.02, mde, delta=0.0001)

    def test_continuous_mde_matches_declared_power(self) -> None:
        mde = continuous_minimum_detectable_effect(
            standard_deviation=2.5, subjects_per_variant=1_000, power=0.80
        )
        self.assertAlmostEqual(
            0.80,
            continuous_test_power(
                standard_deviation=2.5,
                absolute_effect=mde,
                subjects_per_variant=1_000,
            ),
            delta=0.001,
        )

    def test_analytical_binary_power_agrees_with_seeded_simulation(self) -> None:
        seed = 20260924
        trials = 700
        sample_size = 500
        baseline = 0.10
        effect = 0.04
        rng = random.Random(seed)
        detections = 0
        for _ in range(trials):
            control = sum(rng.random() < baseline for _ in range(sample_size))
            treatment = sum(rng.random() < baseline + effect for _ in range(sample_size))
            if analyze_binary_metric(
                control_exposed=sample_size,
                control_converted=control,
                treatment_exposed=sample_size,
                treatment_converted=treatment,
            ).p_value < 0.05:
                detections += 1
        simulated_power = detections / trials
        analytical_power = binary_test_power(
            baseline_rate=baseline,
            absolute_effect=effect,
            subjects_per_variant=sample_size,
        )
        self.assertAlmostEqual(analytical_power, simulated_power, delta=0.06)


class DecisionIntegrityTests(unittest.TestCase):
    def test_binary_recovers_positive_zero_and_negative_effects(self) -> None:
        scenarios = (
            (1_000, 1_200, "treatment", 0.02),
            (1_000, 1_000, "no_detected_difference", 0.0),
            (1_000, 800, "control", -0.02),
        )
        for control, treatment, recommendation, effect in scenarios:
            with self.subTest(effect=effect):
                decision = evaluate_binary_decision(
                    control_exposed=10_000,
                    control_converted=control,
                    treatment_exposed=10_000,
                    treatment_converted=treatment,
                    minimum_sample_size_per_variant=1_000,
                )
                self.assertTrue(decision.eligible_for_decision)
                self.assertEqual(recommendation, decision.recommendation)
                self.assertAlmostEqual(effect, decision.readout.absolute_lift)

    def test_rare_and_common_binary_outcomes_have_finite_uncertainty(self) -> None:
        for converted in (100, 5_000):
            with self.subTest(converted=converted):
                readout = analyze_binary_metric(
                    control_exposed=10_000,
                    control_converted=converted,
                    treatment_exposed=10_000,
                    treatment_converted=converted + 20,
                )
                self.assertTrue(math.isfinite(readout.standard_error))
                self.assertTrue(0 <= readout.p_value <= 1)

    def test_srm_injection_suppresses_recommendation(self) -> None:
        decision = evaluate_binary_decision(
            control_exposed=9_000,
            control_converted=900,
            treatment_exposed=1_000,
            treatment_converted=200,
            minimum_sample_size_per_variant=500,
        )
        self.assertFalse(decision.eligible_for_decision)
        self.assertIsNone(decision.recommendation)
        self.assertIn("sample_ratio_mismatch", decision.integrity_failures)

    def test_guardrail_direction_threshold_and_decision_suppression(self) -> None:
        crash_rule = GuardrailRule(
            "crash_rate", maximum_regression=0.002, direction="lower_is_better"
        )
        evaluation = evaluate_guardrail(
            rule=crash_rule, control_value=0.010, treatment_value=0.015
        )
        self.assertTrue(evaluation.breached)
        self.assertAlmostEqual(0.005, evaluation.observed_regression)
        decision = evaluate_binary_decision(
            control_exposed=10_000,
            control_converted=1_000,
            treatment_exposed=10_000,
            treatment_converted=1_200,
            minimum_sample_size_per_variant=1_000,
            guardrail_evaluations=(evaluation,),
        )
        self.assertIsNone(decision.recommendation)
        self.assertIn("guardrail_breach:crash_rate", decision.integrity_failures)
        self.assertEqual((evaluation,), decision.guardrails)

    def test_repeated_unadjusted_peeking_warns_and_suppresses_recommendation(self) -> None:
        decision = evaluate_binary_decision(
            control_exposed=10_000,
            control_converted=1_000,
            treatment_exposed=10_000,
            treatment_converted=1_200,
            minimum_sample_size_per_variant=1_000,
            analysis_look=2,
        )
        self.assertFalse(decision.eligible_for_decision)
        self.assertIsNone(decision.recommendation)
        self.assertIn("unadjusted_sequential_monitoring", decision.warnings)
        self.assertIn("unadjusted_sequential_monitoring", decision.integrity_failures)

    def test_adjusted_sequential_look_does_not_raise_unadjusted_warning(self) -> None:
        warnings = inference_warnings(
            analysis_look=2,
            planned_looks=3,
            sequential_adjustment="bonferroni-v1",
        )
        self.assertNotIn("unadjusted_sequential_monitoring", warnings)
        decision = evaluate_binary_decision(
            control_exposed=10_000,
            control_converted=1_000,
            treatment_exposed=10_000,
            treatment_converted=1_200,
            minimum_sample_size_per_variant=1_000,
            analysis_look=2,
            planned_looks=3,
            sequential_adjustment="bonferroni-v1",
        )
        self.assertAlmostEqual(0.05 / 3, decision.readout.method_metadata.alpha)

    def test_unknown_sequential_adjustment_cannot_bypass_peeking_gate(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_binary_decision(
                control_exposed=1_000,
                control_converted=100,
                treatment_exposed=1_000,
                treatment_converted=120,
                minimum_sample_size_per_variant=100,
                analysis_look=2,
                planned_looks=3,
                sequential_adjustment="unverified-method",
            )

    def test_segment_multiplicity_is_visible(self) -> None:
        decision = evaluate_binary_decision(
            control_exposed=10_000,
            control_converted=1_000,
            treatment_exposed=10_000,
            treatment_converted=1_000,
            minimum_sample_size_per_variant=1_000,
            segment_comparisons=8,
        )
        self.assertIn("unadjusted_segment_multiplicity", decision.warnings)

    def test_heavy_tailed_continuous_outcomes_warn_and_block_normal_decision(self) -> None:
        control = [0.0] * 99 + [1_000.0]
        treatment = [1.0] * 99 + [1_001.0]
        decision = evaluate_continuous_decision(
            control_values=control,
            treatment_values=treatment,
            minimum_sample_size_per_variant=50,
        )
        self.assertFalse(decision.eligible_for_decision)
        self.assertIsNone(decision.recommendation)
        self.assertIn("heavy_tailed_outcomes:control", decision.warnings)
        self.assertIn("heavy_tailed_outcomes:control", decision.integrity_failures)

    def test_continuous_recovers_positive_zero_and_negative_effects(self) -> None:
        control = tuple(float(index % 10) for index in range(1_000))
        for shift, expected in ((1.0, "treatment"), (0.0, "no_detected_difference"), (-1.0, "control")):
            with self.subTest(shift=shift):
                treatment = tuple(value + shift for value in control)
                decision = evaluate_continuous_decision(
                    control_values=control,
                    treatment_values=treatment,
                    minimum_sample_size_per_variant=500,
                )
                self.assertTrue(decision.eligible_for_decision)
                self.assertEqual(expected, decision.recommendation)
                self.assertAlmostEqual(shift, decision.readout.absolute_lift)
                self.assertIsNotNone(decision.readout.relative_confidence_interval_95)
                self.assertFalse(
                    any(warning.startswith("heavy_tailed_outcomes") for warning in decision.warnings)
                )

    def test_effect_recovery_reports_bias_and_rmse_for_signed_truth(self) -> None:
        for truth in (-0.02, 0.0, 0.02):
            with self.subTest(truth=truth):
                recovery = summarize_effect_recovery(
                    estimates=(truth - 0.001, truth, truth + 0.001), true_effect=truth
                )
                self.assertAlmostEqual(0.0, recovery.bias)
                self.assertGreaterEqual(recovery.root_mean_squared_error, 0)


if __name__ == "__main__":
    unittest.main()
