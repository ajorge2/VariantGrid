from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Literal


Comparison = Literal["at_least", "at_most"]


@dataclass(frozen=True)
class PreRegisteredGate:
    """One frozen numerical condition in a ship/no-ship policy evaluation."""

    name: str
    metric: str
    observed_value: float
    comparison: Comparison
    threshold: float

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.metric.strip():
            raise ValueError("gate name and metric are required")
        if self.comparison not in {"at_least", "at_most"}:
            raise ValueError("comparison must be at_least or at_most")
        if not isfinite(self.observed_value) or not isfinite(self.threshold):
            raise ValueError("gate values must be finite")


@dataclass(frozen=True)
class GateEvaluation:
    name: str
    metric: str
    observed_value: float
    comparison: Comparison
    threshold: float
    passed: bool


@dataclass(frozen=True)
class PreRegisteredDecision:
    """Decision result for an offline or online policy with frozen gates.

    This is deliberately distinct from ``DecisionReadout``. The latter owns
    randomized binary/continuous inference, while this type supports completed
    offline evaluations whose observations were produced by another validated
    measurement process (for example, blinded human review of model policies).
    """

    change: str
    decision: Literal["ship", "no-ship"] | None
    eligible_for_decision: bool
    evidence_status: Literal["complete", "incomplete"]
    gates: tuple[GateEvaluation, ...]
    failed_gates: tuple[str, ...]
    integrity_failures: tuple[str, ...]


def evaluate_pre_registered_gates(
    *,
    change: str,
    gates: Iterable[PreRegisteredGate],
    evidence_complete: bool,
    integrity_failures: Iterable[str] = (),
) -> PreRegisteredDecision:
    """Apply frozen gates without pretending the inputs are randomized arms.

    Incomplete or integrity-failed evidence withholds a decision. Complete
    evidence returns ``ship`` only when every pre-registered gate passes;
    otherwise it returns the affirmative decision ``no-ship``.
    """

    if not change.strip():
        raise ValueError("change is required")
    gate_list = tuple(gates)
    if not gate_list:
        raise ValueError("at least one pre-registered gate is required")
    names = [gate.name for gate in gate_list]
    if len(names) != len(set(names)):
        raise ValueError("pre-registered gate names must be unique")

    evaluations = tuple(
        GateEvaluation(
            name=gate.name,
            metric=gate.metric,
            observed_value=gate.observed_value,
            comparison=gate.comparison,
            threshold=gate.threshold,
            passed=(
                gate.observed_value >= gate.threshold
                if gate.comparison == "at_least"
                else gate.observed_value <= gate.threshold
            ),
        )
        for gate in gate_list
    )
    failures = tuple(dict.fromkeys(value for value in integrity_failures if value.strip()))
    failed_gates = tuple(gate.name for gate in evaluations if not gate.passed)
    eligible = evidence_complete and not failures
    decision: Literal["ship", "no-ship"] | None = None
    if eligible:
        decision = "ship" if not failed_gates else "no-ship"
    return PreRegisteredDecision(
        change=change,
        decision=decision,
        eligible_for_decision=eligible,
        evidence_status="complete" if evidence_complete else "incomplete",
        gates=evaluations,
        failed_gates=failed_gates,
        integrity_failures=failures,
    )


__all__ = [
    "Comparison",
    "GateEvaluation",
    "PreRegisteredDecision",
    "PreRegisteredGate",
    "evaluate_pre_registered_gates",
]
