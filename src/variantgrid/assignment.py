from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping

from .models import Experiment, VariantState


@dataclass(frozen=True)
class AssignmentDiagnostics:
    """Inspectable inputs and bucket boundaries for one assignment decision.

    The raw digest is deliberately not exposed. ``bucket`` and the selected
    interval are sufficient to explain a fixed-weight decision without making
    downstream code depend on hash implementation details.
    """

    bucket: float | None
    interval_start: float | None
    interval_end: float | None
    total_weight: float
    selected_weight: float | None
    eligibility_rule: str
    eligibility_source: str


@dataclass(frozen=True)
class AssignmentResult:
    experiment_key: str
    experiment_version: int
    state: VariantState | None
    state_id: str | None
    assignment_probability: float
    algorithm_version: str
    policy_version: str
    eligible: bool
    fallback_values: Mapping[str, Any] | None = None
    reason: str | None = None
    diagnostics: AssignmentDiagnostics | None = None

    def __post_init__(self) -> None:
        if self.fallback_values is not None:
            object.__setattr__(
                self,
                "fallback_values",
                MappingProxyType(dict(self.fallback_values)),
            )


class AssignmentEngine:
    """Deterministic, version-isolated weighted assignment."""

    _SUPPORTED_ALGORITHMS = frozenset({"sha256-v1"})
    _SUPPORTED_POLICY_PREFIX = "fixed-weight-v"

    def __init__(self, experiment: Experiment) -> None:
        self.experiment = experiment
        if experiment.assignment_algorithm_version not in self._SUPPORTED_ALGORITHMS:
            raise ValueError(
                "Unsupported assignment algorithm version: "
                f"{experiment.assignment_algorithm_version!r}"
            )
        policy_number = experiment.policy_version.removeprefix(self._SUPPORTED_POLICY_PREFIX)
        if (
            not experiment.policy_version.startswith(self._SUPPORTED_POLICY_PREFIX)
            or not policy_number.isdigit()
            or int(policy_number) < 1
        ):
            raise ValueError(f"Unsupported assignment policy version: {experiment.policy_version!r}")
        if not experiment.states:
            raise ValueError("Assignment requires at least one state")
        if any(not isfinite(state.weight) or state.weight <= 0 for state in experiment.states):
            raise ValueError("Assignment weights must be finite and positive")
        self._total_weight = sum(state.weight for state in experiment.states)
        if not isfinite(self._total_weight) or self._total_weight <= 0:
            raise ValueError("Total assignment weight must be finite and positive")
        running = 0.0
        self._thresholds: list[float] = []
        for state in experiment.states:
            running += state.weight / self._total_weight
            self._thresholds.append(running)
        self._thresholds[-1] = 1.0
        self._token_prefix = (
            f"{self.experiment.key}:{self.experiment.version}:"
            f"{self.experiment.salt}:{self.experiment.assignment_algorithm_version}:"
            f"{self.experiment.policy_version}:"
        ).encode("utf-8")

    def _select(self, subject_id: str) -> tuple[VariantState, float, float, float]:
        if not subject_id:
            raise ValueError("subject_id is required")
        # This exact token format is the public contract for sha256-v1. Future
        # formats must use a new algorithm version rather than changing this one.
        token = self._token_prefix + subject_id.encode("utf-8")
        digest = sha256(token).digest()
        bucket = int.from_bytes(digest[:8], "big") / 2**64
        index = min(bisect_right(self._thresholds, bucket), len(self.experiment.states) - 1)
        interval_start = 0.0 if index == 0 else self._thresholds[index - 1]
        return self.experiment.states[index], bucket, interval_start, self._thresholds[index]

    def assign(self, subject_id: str) -> VariantState:
        # Keep the local-evaluation hot path allocation-light. The token must
        # remain byte-for-byte identical to `_select`; parity fixtures guard it.
        if not subject_id:
            raise ValueError("subject_id is required")
        token = self._token_prefix + subject_id.encode("utf-8")
        digest = sha256(token).digest()
        bucket = int.from_bytes(digest[:8], "big") / 2**64
        index = min(bisect_right(self._thresholds, bucket), len(self.experiment.states) - 1)
        return self.experiment.states[index]

    def assign_with_metadata(
        self,
        subject_id: str,
        *,
        eligible: bool | None = None,
        fallback_values: Mapping[str, Any] | None = None,
    ) -> AssignmentResult:
        """Assign an eligible subject or return an explicit, inspectable fallback."""
        if not subject_id:
            raise ValueError("subject_id is required")
        eligibility_source = "caller" if eligible is not None else "experiment_rule"
        ineligibility_reason = "subject_ineligible"
        if self.experiment.eligibility_rule == "none":
            # A disabled experiment cannot be re-enabled by an untrusted caller.
            eligible = False
            eligibility_source = "experiment_rule"
        elif eligible is None:
            if self.experiment.eligibility_rule == "all":
                eligible = True
            else:
                # Arbitrary eligibility expressions require a caller that can
                # evaluate them against trusted subject attributes. Failing
                # closed prevents accidental exposure when that context is absent.
                eligible = False
                ineligibility_reason = "eligibility_context_required"
        if not eligible:
            return AssignmentResult(
                experiment_key=self.experiment.key,
                experiment_version=self.experiment.version,
                state=None,
                state_id=None,
                assignment_probability=0.0,
                algorithm_version=self.experiment.assignment_algorithm_version,
                policy_version=self.experiment.policy_version,
                eligible=False,
                fallback_values=fallback_values,
                reason=ineligibility_reason,
                diagnostics=AssignmentDiagnostics(
                    bucket=None,
                    interval_start=None,
                    interval_end=None,
                    total_weight=self._total_weight,
                    selected_weight=None,
                    eligibility_rule=self.experiment.eligibility_rule,
                    eligibility_source=eligibility_source,
                ),
            )
        state, bucket, interval_start, interval_end = self._select(subject_id)
        return AssignmentResult(
            experiment_key=self.experiment.key,
            experiment_version=self.experiment.version,
            state=state,
            state_id=state.state_id,
            assignment_probability=state.weight / self._total_weight,
            algorithm_version=self.experiment.assignment_algorithm_version,
            policy_version=self.experiment.policy_version,
            eligible=True,
            diagnostics=AssignmentDiagnostics(
                bucket=bucket,
                interval_start=interval_start,
                interval_end=interval_end,
                total_weight=self._total_weight,
                selected_weight=state.weight,
                eligibility_rule=self.experiment.eligibility_rule,
                eligibility_source=eligibility_source,
            ),
        )
