from __future__ import annotations

import unittest

from variantgrid.policy import PreRegisteredGate, evaluate_pre_registered_gates


class PreRegisteredPolicyTests(unittest.TestCase):
    def test_complete_evidence_returns_no_ship_when_any_frozen_gate_fails(self) -> None:
        decision = evaluate_pre_registered_gates(
            change="lower a model acceptance threshold",
            evidence_complete=True,
            gates=(
                PreRegisteredGate("coverage", "accepted_coverage", 0.232, "at_least", 0.20),
                PreRegisteredGate("precision", "strict_precision", 0.686, "at_least", 0.80),
            ),
        )
        self.assertTrue(decision.eligible_for_decision)
        self.assertEqual("no-ship", decision.decision)
        self.assertEqual(("precision",), decision.failed_gates)

    def test_complete_evidence_ships_only_when_every_gate_passes(self) -> None:
        decision = evaluate_pre_registered_gates(
            change="lower a model acceptance threshold",
            evidence_complete=True,
            gates=(
                PreRegisteredGate("coverage", "accepted_coverage", 0.25, "at_least", 0.20),
                PreRegisteredGate("precision", "strict_precision", 0.82, "at_least", 0.80),
                PreRegisteredGate("error", "error_rate", 0.04, "at_most", 0.05),
            ),
        )
        self.assertEqual("ship", decision.decision)
        self.assertEqual((), decision.failed_gates)

    def test_incomplete_or_integrity_failed_evidence_withholds_the_decision(self) -> None:
        gate = PreRegisteredGate("coverage", "accepted_coverage", 0.25, "at_least", 0.20)
        incomplete = evaluate_pre_registered_gates(
            change="change", gates=(gate,), evidence_complete=False
        )
        failed = evaluate_pre_registered_gates(
            change="change",
            gates=(gate,),
            evidence_complete=True,
            integrity_failures=("source_hash_mismatch",),
        )
        self.assertFalse(incomplete.eligible_for_decision)
        self.assertIsNone(incomplete.decision)
        self.assertFalse(failed.eligible_for_decision)
        self.assertIsNone(failed.decision)
        self.assertEqual(("source_hash_mismatch",), failed.integrity_failures)


if __name__ == "__main__":
    unittest.main()
