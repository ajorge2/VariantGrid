from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from math import erf, exp, isfinite, log, sqrt
from statistics import NormalDist, mean, variance
from typing import Iterable, Literal


DEFAULT_ALPHA = 0.05
DEFAULT_SRM_ALPHA = 0.001
DEFAULT_HEAVY_TAIL_Z_THRESHOLD = 8.0


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def _two_sided_normal_p_value(z_score: float) -> float:
    return max(0.0, min(1.0, 2 * (1 - _normal_cdf(abs(z_score)))))


def _critical_value(alpha: float) -> float:
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    return NormalDist().inv_cdf(1 - alpha / 2)


@dataclass(frozen=True)
class StatisticalMethodMetadata:
    """Persistable description of the method and assumptions behind a readout."""

    method: str
    version: str
    estimand: str
    test: str
    interval: str
    alpha: float = DEFAULT_ALPHA
    sidedness: Literal["two-sided"] = "two-sided"
    stopping_rule: str = "fixed_horizon"
    planned_looks: int = 1
    multiplicity_correction: str = "none"
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.method.strip() or not self.version.strip():
            raise ValueError("method and version are required")
        if not 0 < self.alpha < 1:
            raise ValueError("alpha must be between zero and one")
        if self.planned_looks < 1:
            raise ValueError("planned_looks must be at least one")

    @property
    def method_id(self) -> str:
        return f"{self.method}-{self.version}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, payload: str) -> StatisticalMethodMetadata:
        raw = json.loads(payload)
        raw["assumptions"] = tuple(raw.get("assumptions", ()))
        return cls(**raw)


@dataclass(frozen=True)
class AnalysisPlan:
    """Predeclared decision inputs that can be stored with an experiment version."""

    primary_metric: str
    metric_type: Literal["binary", "continuous"]
    alpha: float = DEFAULT_ALPHA
    target_power: float = 0.80
    minimum_sample_size_per_variant: int = 1
    expected_treatment_share: float = 0.5
    srm_alpha: float = DEFAULT_SRM_ALPHA
    stopping_rule: str = "fixed_horizon"
    planned_looks: int = 1
    segment_comparisons: int = 1
    statistical_method: str = "default"

    def __post_init__(self) -> None:
        if not self.primary_metric.strip():
            raise ValueError("primary_metric is required")
        if not 0 < self.alpha < 1 or not 0 < self.target_power < 1:
            raise ValueError("alpha and target_power must be between zero and one")
        if self.minimum_sample_size_per_variant < 1:
            raise ValueError("minimum_sample_size_per_variant must be positive")
        if not 0 < self.expected_treatment_share < 1:
            raise ValueError("expected_treatment_share must be between zero and one")
        if not 0 < self.srm_alpha < 1:
            raise ValueError("srm_alpha must be between zero and one")
        if self.planned_looks < 1 or self.segment_comparisons < 1:
            raise ValueError("planned_looks and segment_comparisons must be positive")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_json(cls, payload: str) -> AnalysisPlan:
        return cls(**json.loads(payload))


@dataclass(frozen=True)
class GuardrailRule:
    metric: str
    maximum_regression: float
    direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise ValueError("guardrail metric is required")
        if not isfinite(self.maximum_regression) or self.maximum_regression < 0:
            raise ValueError("maximum_regression must be a finite non-negative number")


@dataclass(frozen=True)
class GuardrailEvaluation:
    metric: str
    control_value: float
    treatment_value: float
    observed_regression: float
    maximum_regression: float
    breached: bool
    direction: str


@dataclass(frozen=True)
class Readout:
    control_rate: float
    treatment_rate: float
    absolute_lift: float
    relative_lift: float
    standard_error: float
    confidence_interval_95: tuple[float, float]
    p_value: float
    sample_ratio_mismatch_p_value: float
    statistical_method: str = "two-proportion-z-v1"
    method_metadata: StatisticalMethodMetadata | None = None
    warnings: tuple[str, ...] = ()
    relative_confidence_interval_95: tuple[float, float] | None = None


@dataclass(frozen=True)
class ContinuousReadout:
    control_mean: float
    treatment_mean: float
    absolute_lift: float
    relative_lift: float
    standard_error: float
    confidence_interval_95: tuple[float, float]
    p_value: float
    statistical_method: str = "welch-normal-v1"
    method_metadata: StatisticalMethodMetadata | None = None
    warnings: tuple[str, ...] = ()
    relative_confidence_interval_95: tuple[float, float] | None = None


@dataclass(frozen=True)
class DecisionReadout:
    readout: Readout | ContinuousReadout
    eligible_for_decision: bool
    recommendation: str | None
    integrity_failures: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    guardrails: tuple[GuardrailEvaluation, ...] = ()


@dataclass(frozen=True)
class EffectRecovery:
    true_effect: float
    mean_estimate: float
    bias: float
    root_mean_squared_error: float
    estimates: int


def binary_method_metadata(
    *, alpha: float = DEFAULT_ALPHA, stopping_rule: str = "fixed_horizon", planned_looks: int = 1
) -> StatisticalMethodMetadata:
    return StatisticalMethodMetadata(
        method="two-proportion-z",
        version="v1",
        estimand="treatment conversion rate minus control conversion rate",
        test="unpooled two-proportion normal test",
        interval="unpooled Wald confidence interval",
        alpha=alpha,
        stopping_rule=stopping_rule,
        planned_looks=planned_looks,
        assumptions=(
            "independent subjects",
            "exposure-gated version-clean attribution",
            "normal approximation is adequate",
            "fixed-horizon inference unless an adjusted method is declared",
        ),
    )


def continuous_method_metadata(
    *, alpha: float = DEFAULT_ALPHA, stopping_rule: str = "fixed_horizon", planned_looks: int = 1
) -> StatisticalMethodMetadata:
    return StatisticalMethodMetadata(
        method="welch-normal",
        version="v1",
        estimand="treatment mean minus control mean",
        test="Welch standard error with two-sided normal reference",
        interval="normal approximation confidence interval",
        alpha=alpha,
        stopping_rule=stopping_rule,
        planned_looks=planned_looks,
        assumptions=(
            "independent subjects",
            "finite variance",
            "normal approximation is adequate",
            "fixed-horizon inference unless an adjusted method is declared",
        ),
    )


def analyze_binary_metric(
    *,
    control_exposed: int,
    control_converted: int,
    treatment_exposed: int,
    treatment_converted: int,
    expected_treatment_share: float = 0.5,
    alpha: float = DEFAULT_ALPHA,
    stopping_rule: str = "fixed_horizon",
    planned_looks: int = 1,
) -> Readout:
    if min(control_exposed, treatment_exposed) <= 0:
        raise ValueError("Both variants require exposed subjects")
    if not 0 <= control_converted <= control_exposed:
        raise ValueError("control_converted must be between zero and control_exposed")
    if not 0 <= treatment_converted <= treatment_exposed:
        raise ValueError("treatment_converted must be between zero and treatment_exposed")
    if not 0 < expected_treatment_share < 1:
        raise ValueError("Expected share must be between zero and one")
    critical_value = _critical_value(alpha)

    control_rate = control_converted / control_exposed
    treatment_rate = treatment_converted / treatment_exposed
    difference = treatment_rate - control_rate
    standard_error = sqrt(
        control_rate * (1 - control_rate) / control_exposed
        + treatment_rate * (1 - treatment_rate) / treatment_exposed
    )
    warnings: list[str] = []
    if standard_error == 0:
        warnings.append("degenerate_variance")
        p_value = 1.0 if difference == 0 else 0.0
    else:
        p_value = _two_sided_normal_p_value(difference / standard_error)
    relative_lift = difference / control_rate if control_rate else float("inf")
    relative_interval: tuple[float, float] | None = None
    if control_converted > 0 and treatment_converted > 0:
        risk_ratio = treatment_rate / control_rate
        log_risk_ratio_standard_error = sqrt(
            1 / treatment_converted
            - 1 / treatment_exposed
            + 1 / control_converted
            - 1 / control_exposed
        )
        relative_margin = critical_value * log_risk_ratio_standard_error
        relative_interval = (
            exp(log(risk_ratio) - relative_margin) - 1,
            exp(log(risk_ratio) + relative_margin) - 1,
        )
    else:
        warnings.append("relative_effect_interval_unavailable")

    total = control_exposed + treatment_exposed
    expected_treatment = total * expected_treatment_share
    expected_control = total - expected_treatment
    chi_square = (
        (treatment_exposed - expected_treatment) ** 2 / expected_treatment
        + (control_exposed - expected_control) ** 2 / expected_control
    )
    # A chi-square with one degree of freedom is the square of a standard normal.
    srm_p = _two_sided_normal_p_value(sqrt(chi_square))
    margin = critical_value * standard_error
    metadata = binary_method_metadata(
        alpha=alpha, stopping_rule=stopping_rule, planned_looks=planned_looks
    )
    return Readout(
        control_rate=control_rate,
        treatment_rate=treatment_rate,
        absolute_lift=difference,
        relative_lift=relative_lift,
        standard_error=standard_error,
        confidence_interval_95=(difference - margin, difference + margin),
        p_value=p_value,
        sample_ratio_mismatch_p_value=srm_p,
        statistical_method=metadata.method_id,
        method_metadata=metadata,
        warnings=tuple(warnings),
        relative_confidence_interval_95=relative_interval,
    )


def _heavy_tail_warnings(values: tuple[float, ...], label: str) -> tuple[str, ...]:
    if len(values) < 8:
        return ()
    sample_variance = variance(values)
    if sample_variance == 0:
        return ()
    sample_mean = mean(values)
    maximum_standardized_distance = max(
        abs(value - sample_mean) / sqrt(sample_variance) for value in values
    )
    if maximum_standardized_distance > DEFAULT_HEAVY_TAIL_Z_THRESHOLD:
        return (f"heavy_tailed_outcomes:{label}",)
    return ()


def analyze_continuous_metric(
    *,
    control_values: Iterable[float],
    treatment_values: Iterable[float],
    alpha: float = DEFAULT_ALPHA,
    stopping_rule: str = "fixed_horizon",
    planned_looks: int = 1,
) -> ContinuousReadout:
    control = tuple(float(value) for value in control_values)
    treatment = tuple(float(value) for value in treatment_values)
    if len(control) < 2 or len(treatment) < 2:
        raise ValueError("Continuous readouts require at least two observations per variant")
    if not all(isfinite(value) for value in (*control, *treatment)):
        raise ValueError("Continuous observations must be finite")
    critical_value = _critical_value(alpha)
    control_mean = mean(control)
    treatment_mean = mean(treatment)
    difference = treatment_mean - control_mean
    control_variance = variance(control)
    treatment_variance = variance(treatment)
    standard_error = sqrt(control_variance / len(control) + treatment_variance / len(treatment))
    warnings = [*_heavy_tail_warnings(control, "control"), *_heavy_tail_warnings(treatment, "treatment")]
    if standard_error == 0:
        warnings.append("degenerate_variance")
        p_value = 1.0 if difference == 0 else 0.0
    else:
        p_value = _two_sided_normal_p_value(difference / standard_error)
    margin = critical_value * standard_error
    relative_interval: tuple[float, float] | None = None
    if control_mean != 0:
        relative_lift = difference / control_mean
        relative_standard_error = sqrt(
            treatment_variance / (len(treatment) * control_mean**2)
            + treatment_mean**2
            * control_variance
            / (len(control) * control_mean**4)
        )
        relative_interval = (
            relative_lift - critical_value * relative_standard_error,
            relative_lift + critical_value * relative_standard_error,
        )
    else:
        relative_lift = float("inf")
        warnings.append("relative_effect_interval_unavailable")
    metadata = continuous_method_metadata(
        alpha=alpha, stopping_rule=stopping_rule, planned_looks=planned_looks
    )
    return ContinuousReadout(
        control_mean=control_mean,
        treatment_mean=treatment_mean,
        absolute_lift=difference,
        relative_lift=relative_lift,
        standard_error=standard_error,
        confidence_interval_95=(difference - margin, difference + margin),
        p_value=p_value,
        statistical_method=metadata.method_id,
        method_metadata=metadata,
        warnings=tuple(warnings),
        relative_confidence_interval_95=relative_interval,
    )


def binary_test_power(
    *,
    baseline_rate: float,
    absolute_effect: float,
    subjects_per_variant: int,
    alpha: float = DEFAULT_ALPHA,
) -> float:
    """Approximate fixed-horizon power for a two-sided binary comparison."""
    if not 0 < baseline_rate < 1:
        raise ValueError("baseline_rate must be between zero and one")
    treatment_rate = baseline_rate + absolute_effect
    if not 0 <= treatment_rate <= 1:
        raise ValueError("baseline_rate + absolute_effect must be between zero and one")
    if subjects_per_variant <= 0:
        raise ValueError("subjects_per_variant must be positive")
    critical_value = _critical_value(alpha)
    standard_error = sqrt(
        baseline_rate * (1 - baseline_rate) / subjects_per_variant
        + treatment_rate * (1 - treatment_rate) / subjects_per_variant
    )
    if standard_error == 0:
        return 0.0 if absolute_effect == 0 else 1.0
    noncentrality = abs(absolute_effect) / standard_error
    power = 1 - _normal_cdf(critical_value - noncentrality) + _normal_cdf(
        -critical_value - noncentrality
    )
    return max(0.0, min(1.0, power))


def continuous_test_power(
    *,
    standard_deviation: float,
    absolute_effect: float,
    subjects_per_variant: int,
    alpha: float = DEFAULT_ALPHA,
) -> float:
    if not isfinite(standard_deviation) or standard_deviation <= 0:
        raise ValueError("standard_deviation must be a finite positive number")
    if subjects_per_variant <= 0:
        raise ValueError("subjects_per_variant must be positive")
    critical_value = _critical_value(alpha)
    noncentrality = abs(absolute_effect) / (
        standard_deviation * sqrt(2 / subjects_per_variant)
    )
    power = 1 - _normal_cdf(critical_value - noncentrality) + _normal_cdf(
        -critical_value - noncentrality
    )
    return max(0.0, min(1.0, power))


def minimum_detectable_effect(
    *,
    baseline_rate: float,
    subjects_per_variant: int,
    alpha: float = DEFAULT_ALPHA,
    power: float = 0.80,
) -> float:
    """Numerically invert the binary fixed-horizon power calculation."""
    if not 0 < baseline_rate < 1:
        raise ValueError("baseline_rate must be between zero and one")
    if subjects_per_variant <= 0 or not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("sample size, alpha, and power must be valid")
    low = 0.0
    high = 1 - baseline_rate
    if binary_test_power(
        baseline_rate=baseline_rate,
        absolute_effect=high,
        subjects_per_variant=subjects_per_variant,
        alpha=alpha,
    ) < power:
        raise ValueError("target power is unattainable for the requested sample size")
    for _ in range(64):
        midpoint = (low + high) / 2
        observed_power = binary_test_power(
            baseline_rate=baseline_rate,
            absolute_effect=midpoint,
            subjects_per_variant=subjects_per_variant,
            alpha=alpha,
        )
        if observed_power < power:
            low = midpoint
        else:
            high = midpoint
    return high


def continuous_minimum_detectable_effect(
    *,
    standard_deviation: float,
    subjects_per_variant: int,
    alpha: float = DEFAULT_ALPHA,
    power: float = 0.80,
) -> float:
    if not isfinite(standard_deviation) or standard_deviation <= 0:
        raise ValueError("standard_deviation must be a finite positive number")
    if subjects_per_variant <= 0 or not 0 < power < 1:
        raise ValueError("sample size and power must be valid")
    z_alpha = _critical_value(alpha)
    z_power = NormalDist().inv_cdf(power)
    return (z_alpha + z_power) * standard_deviation * sqrt(2 / subjects_per_variant)


def required_sample_size(
    *, baseline_rate: float, absolute_effect: float, alpha: float = DEFAULT_ALPHA, power: float = 0.80
) -> int:
    if not 0 < baseline_rate < 1:
        raise ValueError("baseline_rate must be between zero and one")
    if absolute_effect <= 0 or baseline_rate + absolute_effect > 1:
        raise ValueError("absolute_effect must be positive and keep the treatment rate at most one")
    if not 0 < power < 1:
        raise ValueError("power must be between zero and one")
    low, high = 1, 2
    while binary_test_power(
        baseline_rate=baseline_rate,
        absolute_effect=absolute_effect,
        subjects_per_variant=high,
        alpha=alpha,
    ) < power:
        low, high = high, high * 2
    while low < high:
        midpoint = (low + high) // 2
        observed_power = binary_test_power(
            baseline_rate=baseline_rate,
            absolute_effect=absolute_effect,
            subjects_per_variant=midpoint,
            alpha=alpha,
        )
        if observed_power >= power:
            high = midpoint
        else:
            low = midpoint + 1
    return low


def evaluate_guardrail(
    *, rule: GuardrailRule, control_value: float, treatment_value: float
) -> GuardrailEvaluation:
    if not isfinite(control_value) or not isfinite(treatment_value):
        raise ValueError("guardrail values must be finite")
    if rule.direction == "higher_is_better":
        regression = control_value - treatment_value
    else:
        regression = treatment_value - control_value
    return GuardrailEvaluation(
        metric=rule.metric,
        control_value=control_value,
        treatment_value=treatment_value,
        observed_regression=regression,
        maximum_regression=rule.maximum_regression,
        breached=regression > rule.maximum_regression,
        direction=rule.direction,
    )


def inference_warnings(
    *,
    analysis_look: int = 1,
    planned_looks: int = 1,
    sequential_adjustment: str | None = None,
    segment_comparisons: int = 1,
    multiplicity_correction: str | None = None,
) -> tuple[str, ...]:
    if analysis_look < 1 or planned_looks < 1 or segment_comparisons < 1:
        raise ValueError("analysis_look, planned_looks, and segment_comparisons must be positive")
    warnings: list[str] = []
    if analysis_look > 1 and sequential_adjustment is None:
        warnings.append("unadjusted_sequential_monitoring")
    if analysis_look > planned_looks:
        warnings.append("analysis_look_exceeds_plan")
    if segment_comparisons > 1 and multiplicity_correction is None:
        warnings.append("unadjusted_segment_multiplicity")
    return tuple(warnings)


def _effective_alpha(
    *, alpha: float, sequential_adjustment: str | None, planned_looks: int
) -> float:
    """Return the per-look alpha for the explicitly supported correction."""
    _critical_value(alpha)
    if sequential_adjustment is None:
        return alpha
    if sequential_adjustment not in {"bonferroni", "bonferroni-v1"}:
        raise ValueError("unsupported sequential_adjustment; supported value is bonferroni-v1")
    if planned_looks < 1:
        raise ValueError("planned_looks must be positive")
    return alpha / planned_looks


def summarize_effect_recovery(
    *, estimates: Iterable[float], true_effect: float
) -> EffectRecovery:
    observed = tuple(float(estimate) for estimate in estimates)
    if not observed or not isfinite(true_effect) or not all(isfinite(value) for value in observed):
        raise ValueError("effect recovery requires finite estimates and truth")
    average = mean(observed)
    return EffectRecovery(
        true_effect=true_effect,
        mean_estimate=average,
        bias=average - true_effect,
        root_mean_squared_error=sqrt(mean((value - true_effect) ** 2 for value in observed)),
        estimates=len(observed),
    )


def _decision_warnings_and_failures(
    *,
    readout_warnings: Iterable[str],
    analysis_look: int,
    planned_looks: int,
    sequential_adjustment: str | None,
    segment_comparisons: int,
    multiplicity_correction: str | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    warnings = tuple(readout_warnings) + inference_warnings(
        analysis_look=analysis_look,
        planned_looks=planned_looks,
        sequential_adjustment=sequential_adjustment,
        segment_comparisons=segment_comparisons,
        multiplicity_correction=multiplicity_correction,
    )
    blocking = tuple(
        warning
        for warning in warnings
        if warning.startswith("heavy_tailed_outcomes")
        or warning in {
            "degenerate_variance",
            "unadjusted_sequential_monitoring",
            "analysis_look_exceeds_plan",
        }
    )
    return warnings, blocking


def evaluate_binary_decision(
    *,
    control_exposed: int,
    control_converted: int,
    treatment_exposed: int,
    treatment_converted: int,
    minimum_sample_size_per_variant: int,
    expected_treatment_share: float = 0.5,
    srm_alpha: float = DEFAULT_SRM_ALPHA,
    guardrail_breaches: Iterable[str] = (),
    guardrail_evaluations: Iterable[GuardrailEvaluation] = (),
    alpha: float = DEFAULT_ALPHA,
    analysis_look: int = 1,
    planned_looks: int = 1,
    sequential_adjustment: str | None = None,
    segment_comparisons: int = 1,
    multiplicity_correction: str | None = None,
) -> DecisionReadout:
    if minimum_sample_size_per_variant < 1:
        raise ValueError("minimum_sample_size_per_variant must be positive")
    if not 0 < srm_alpha < 1:
        raise ValueError("srm_alpha must be between zero and one")
    effective_alpha = _effective_alpha(
        alpha=alpha,
        sequential_adjustment=sequential_adjustment,
        planned_looks=planned_looks,
    )
    stopping_rule = sequential_adjustment or "fixed_horizon"
    readout = analyze_binary_metric(
        control_exposed=control_exposed,
        control_converted=control_converted,
        treatment_exposed=treatment_exposed,
        treatment_converted=treatment_converted,
        expected_treatment_share=expected_treatment_share,
        alpha=effective_alpha,
        stopping_rule=stopping_rule,
        planned_looks=planned_looks,
    )
    evaluations = tuple(guardrail_evaluations)
    warnings, warning_failures = _decision_warnings_and_failures(
        readout_warnings=readout.warnings,
        analysis_look=analysis_look,
        planned_looks=planned_looks,
        sequential_adjustment=sequential_adjustment,
        segment_comparisons=segment_comparisons,
        multiplicity_correction=multiplicity_correction,
    )
    failures: list[str] = list(warning_failures)
    if min(control_exposed, treatment_exposed) < minimum_sample_size_per_variant:
        failures.append("insufficient_sample_size")
    if readout.sample_ratio_mismatch_p_value < srm_alpha:
        failures.append("sample_ratio_mismatch")
    failures.extend(f"guardrail_breach:{metric}" for metric in guardrail_breaches)
    failures.extend(
        f"guardrail_breach:{evaluation.metric}" for evaluation in evaluations if evaluation.breached
    )
    failures = list(dict.fromkeys(failures))
    if failures:
        return DecisionReadout(
            readout, False, None, tuple(failures), warnings=warnings, guardrails=evaluations
        )

    low, high = readout.confidence_interval_95
    recommendation = "no_detected_difference"
    if readout.p_value < effective_alpha and low > 0:
        recommendation = "treatment"
    elif readout.p_value < effective_alpha and high < 0:
        recommendation = "control"
    return DecisionReadout(
        readout, True, recommendation, (), warnings=warnings, guardrails=evaluations
    )


def evaluate_continuous_decision(
    *,
    control_values: Iterable[float],
    treatment_values: Iterable[float],
    minimum_sample_size_per_variant: int,
    guardrail_evaluations: Iterable[GuardrailEvaluation] = (),
    alpha: float = DEFAULT_ALPHA,
    analysis_look: int = 1,
    planned_looks: int = 1,
    sequential_adjustment: str | None = None,
    segment_comparisons: int = 1,
    multiplicity_correction: str | None = None,
) -> DecisionReadout:
    control = tuple(control_values)
    treatment = tuple(treatment_values)
    if minimum_sample_size_per_variant < 1:
        raise ValueError("minimum_sample_size_per_variant must be positive")
    effective_alpha = _effective_alpha(
        alpha=alpha,
        sequential_adjustment=sequential_adjustment,
        planned_looks=planned_looks,
    )
    stopping_rule = sequential_adjustment or "fixed_horizon"
    readout = analyze_continuous_metric(
        control_values=control,
        treatment_values=treatment,
        alpha=effective_alpha,
        stopping_rule=stopping_rule,
        planned_looks=planned_looks,
    )
    evaluations = tuple(guardrail_evaluations)
    warnings, warning_failures = _decision_warnings_and_failures(
        readout_warnings=readout.warnings,
        analysis_look=analysis_look,
        planned_looks=planned_looks,
        sequential_adjustment=sequential_adjustment,
        segment_comparisons=segment_comparisons,
        multiplicity_correction=multiplicity_correction,
    )
    failures: list[str] = list(warning_failures)
    if min(len(control), len(treatment)) < minimum_sample_size_per_variant:
        failures.append("insufficient_sample_size")
    failures.extend(
        f"guardrail_breach:{evaluation.metric}" for evaluation in evaluations if evaluation.breached
    )
    failures = list(dict.fromkeys(failures))
    if failures:
        return DecisionReadout(
            readout, False, None, tuple(failures), warnings=warnings, guardrails=evaluations
        )

    low, high = readout.confidence_interval_95
    recommendation = "no_detected_difference"
    if readout.p_value < effective_alpha and low > 0:
        recommendation = "treatment"
    elif readout.p_value < effective_alpha and high < 0:
        recommendation = "control"
    return DecisionReadout(
        readout, True, recommendation, (), warnings=warnings, guardrails=evaluations
    )
