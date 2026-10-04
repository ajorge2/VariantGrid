from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping


class VariableType(str, Enum):
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ENUM = "enum"


def canonical_state_json(values: Mapping[str, Any]) -> str:
    """Serialize a state independently of mapping insertion order."""
    return json.dumps(dict(values), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_state_id(values: Mapping[str, Any]) -> str:
    return sha256(canonical_state_json(values).encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class Factor:
    """A finite typed variable, optionally active only under earlier values.

    Conditional factors use explicit, deterministic semantics: every key in
    ``active_when`` must match before the factor is included in a state. A
    condition may reference only factors declared before it, which prevents
    cycles and order-dependent interpretation.
    """

    name: str
    values: tuple[Any, ...]
    variable_type: VariableType = VariableType.ENUM
    active_when: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.values:
            raise ValueError("Factors require a name and at least one value")
        if len(self.values) != len({canonical_state_json({"value": value}) for value in self.values}):
            raise ValueError("Factor values must be unique")
        validators = {
            VariableType.INTEGER: lambda value: type(value) is int,
            VariableType.FLOAT: lambda value: type(value) in (int, float),
            VariableType.BOOLEAN: lambda value: type(value) is bool,
            VariableType.ENUM: lambda value: isinstance(value, (str, int, float, bool)),
        }
        if not all(validators[self.variable_type](value) for value in self.values):
            raise ValueError(f"Values for {self.name!r} do not match {self.variable_type.value}")
        object.__setattr__(self, "active_when", MappingProxyType(dict(self.active_when)))

    def is_active(self, partial_state: Mapping[str, Any]) -> bool:
        return all(partial_state.get(key) == value for key, value in self.active_when.items())


@dataclass(frozen=True)
class Rule:
    """Require values or exclude a factor combination from the state space.

    ``match_order`` records whether the predicates are an unordered
    combination or an ordered sequence in factor-declaration order. State
    generation is deterministic, so an ordered rule is validated against the
    stored factor order before any states are accepted.
    """

    when: Mapping[str, Any]
    require: Mapping[str, Any] = field(default_factory=dict)
    reason: str = "constraint_not_satisfied"
    effect: str = "require"
    match_order: str = "any_order"
    sequence: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.effect not in {"require", "exclude"}:
            raise ValueError("Rule effect must be require or exclude")
        if self.match_order not in {"any_order", "in_order"}:
            raise ValueError("Rule match_order must be any_order or in_order")
        if self.effect == "require" and not self.require:
            raise ValueError("Rules require a non-empty require clause")
        if self.effect == "exclude" and not self.when:
            raise ValueError("Exclusion rules require at least one factor predicate")
        sequence = tuple(self.sequence)
        if self.match_order == "in_order":
            sequence = sequence or tuple(self.when)
            if len(sequence) != len(set(sequence)) or set(sequence) != set(self.when):
                raise ValueError(
                    "Ordered rule sequence must list every when-clause factor exactly once"
                )
        if not self.reason.strip():
            raise ValueError("Rules require a rejection reason")
        object.__setattr__(self, "when", MappingProxyType(dict(self.when)))
        object.__setattr__(self, "require", MappingProxyType(dict(self.require)))
        object.__setattr__(self, "sequence", sequence)

    def accepts(self, state: Mapping[str, Any]) -> bool:
        triggered = all(state.get(key) == value for key, value in self.when.items())
        if self.effect == "exclude":
            return not triggered
        return not triggered or all(state.get(key) == value for key, value in self.require.items())


@dataclass(frozen=True)
class VariantState:
    key: str
    values: Mapping[str, Any]
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not self.key or self.weight <= 0:
            raise ValueError("Variant states require a key and positive weight")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @property
    def state_id(self) -> str:
        return stable_state_id(self.values)

    @property
    def canonical_json(self) -> str:
        return canonical_state_json(self.values)


@dataclass(frozen=True)
class RejectedState:
    values: Mapping[str, Any]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValueError("Rejected states require at least one reason")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    @property
    def canonical_json(self) -> str:
        return canonical_state_json(self.values)


@dataclass(frozen=True)
class StateGenerationResult:
    accepted: tuple[VariantState, ...]
    rejected: tuple[RejectedState, ...]


def _validate_definition(factors: tuple[Factor, ...], rules: tuple[Rule, ...]) -> None:
    names = [factor.name for factor in factors]
    if not names:
        raise ValueError("At least one factor is required")
    if len(names) != len(set(names)):
        raise ValueError("Factor names must be unique")

    declared: dict[str, Factor] = {}
    for factor in factors:
        for dependency, expected in factor.active_when.items():
            if dependency not in declared:
                raise ValueError(
                    f"Conditional factor {factor.name!r} must reference an earlier factor; "
                    f"unknown or forward dependency: {dependency!r}"
                )
            if expected not in declared[dependency].values:
                raise ValueError(
                    f"Conditional factor {factor.name!r} references unavailable value "
                    f"{dependency}={expected!r}"
                )
        declared[factor.name] = factor

    known = set(names)
    factor_by_name = {factor.name: factor for factor in factors}
    factor_order = {name: index for index, name in enumerate(names)}
    for rule in rules:
        unknown = (set(rule.when) | set(rule.require)) - known
        if unknown:
            raise ValueError(f"Rule references unknown factors: {sorted(unknown)}")
        for name, value in (*rule.when.items(), *rule.require.items()):
            if value not in factor_by_name[name].values:
                raise ValueError(f"Rule references unavailable value {name}={value!r}")
        if rule.match_order == "in_order":
            positions = [factor_order[name] for name in rule.sequence]
            if positions != sorted(positions):
                raise ValueError(
                    "Ordered rule contradicts factor declaration order: "
                    + " -> ".join(rule.sequence)
                )


def generate_state_space(
    factors: Iterable[Factor], rules: Iterable[Rule] = ()
) -> StateGenerationResult:
    """Expand a finite variable definition into ordered valid and rejected states."""
    factor_list = tuple(factors)
    rule_list = tuple(rules)
    _validate_definition(factor_list, rule_list)

    combinations: list[dict[str, Any]] = []

    def expand(index: int, current: dict[str, Any]) -> None:
        if index == len(factor_list):
            combinations.append(dict(current))
            return
        factor = factor_list[index]
        if not factor.is_active(current):
            expand(index + 1, current)
            return
        for value in factor.values:
            current[factor.name] = value
            expand(index + 1, current)
        current.pop(factor.name, None)

    expand(0, {})
    accepted: list[VariantState] = []
    rejected: list[RejectedState] = []
    factor_order = {factor.name: index for index, factor in enumerate(factor_list)}
    for values in combinations:
        reasons = tuple(rule.reason for rule in rule_list if not rule.accepts(values))
        if reasons:
            rejected.append(RejectedState(values, reasons))
            continue
        ordered_names = sorted(values, key=factor_order.__getitem__)
        state_key = "|".join(f"{name}={values[name]}" for name in ordered_names)
        accepted.append(VariantState(state_key, values))

    # Expansion order is deterministic from the stored factor definition. The
    # canonical secondary sort makes replay robust to mapping insertion order.
    accepted.sort(key=lambda state: state.canonical_json)
    rejected.sort(key=lambda state: state.canonical_json)
    return StateGenerationResult(tuple(accepted), tuple(rejected))


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    metric_type: str = "binary"
    role: str = "primary"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Metric name is required")
        if self.metric_type not in {"binary", "continuous"}:
            raise ValueError("Metric type must be binary or continuous")
        if self.role not in {"primary", "secondary"}:
            raise ValueError("Metric role must be primary or secondary")


@dataclass(frozen=True)
class Guardrail:
    metric: str
    maximum_regression: float

    def __post_init__(self) -> None:
        if not self.metric:
            raise ValueError("Guardrail metric is required")
        if self.maximum_regression < 0:
            raise ValueError("Guardrail maximum regression cannot be negative")


@dataclass(frozen=True)
class Experiment:
    key: str
    version: int
    states: tuple[VariantState, ...]
    primary_metric: str
    salt: str
    guardrails: tuple[Guardrail, ...] = field(default_factory=tuple)
    minimum_sample_size: int = 0
    assignment_algorithm_version: str = "sha256-v1"
    policy_version: str = "fixed-weight-v1"
    eligibility_rule: str = "all"
    attribution_window_hours: int = 168
    stopping_rule: str = "fixed-horizon"
    statistical_method: str = "two-proportion-z-v1"
    hypothesis: str = ""
    factors: tuple[Factor, ...] = field(default_factory=tuple)
    rules: tuple[Rule, ...] = field(default_factory=tuple)
    metrics: tuple[MetricDefinition, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.key or self.version < 1 or not self.states or not self.primary_metric:
            raise ValueError("Experiment key, version, states, and primary metric are required")
        keys = [state.key for state in self.states]
        if len(keys) != len(set(keys)):
            raise ValueError("Variant state keys must be unique")
        state_ids = [state.state_id for state in self.states]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("Variant states must have unique canonical values")
        if self.attribution_window_hours <= 0:
            raise ValueError("Attribution window must be positive")
        if self.minimum_sample_size < 0:
            raise ValueError("Minimum sample size cannot be negative")
        if self.factors:
            _validate_definition(self.factors, self.rules)
        metric_names = [metric.name for metric in self.metrics]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("Metric names must be unique")
        if self.metrics and self.primary_metric not in metric_names:
            raise ValueError("Primary metric must be included in metric definitions")

    @classmethod
    def from_factors(
        cls,
        *,
        key: str,
        version: int,
        factors: Iterable[Factor],
        primary_metric: str,
        salt: str,
        rules: Iterable[Rule] = (),
        guardrails: Iterable[Guardrail] = (),
        metrics: Iterable[MetricDefinition] = (),
        hypothesis: str = "",
        minimum_sample_size: int = 0,
        assignment_algorithm_version: str = "sha256-v1",
        policy_version: str = "fixed-weight-v1",
        eligibility_rule: str = "all",
        attribution_window_hours: int = 168,
        stopping_rule: str = "fixed-horizon",
        statistical_method: str = "two-proportion-z-v1",
    ) -> "Experiment":
        factor_list = tuple(factors)
        rule_list = tuple(rules)
        generated = generate_state_space(factor_list, rule_list)
        if not generated.accepted:
            raise ValueError("Constraints eliminated every possible state")
        metric_list = tuple(metrics) or (MetricDefinition(primary_metric),)
        return cls(
            key=key,
            version=version,
            states=generated.accepted,
            primary_metric=primary_metric,
            salt=salt,
            guardrails=tuple(guardrails),
            minimum_sample_size=minimum_sample_size,
            assignment_algorithm_version=assignment_algorithm_version,
            policy_version=policy_version,
            eligibility_rule=eligibility_rule,
            attribution_window_hours=attribution_window_hours,
            stopping_rule=stopping_rule,
            statistical_method=statistical_method,
            hypothesis=hypothesis,
            factors=factor_list,
            rules=rule_list,
            metrics=metric_list,
        )

    def preview_states(self) -> StateGenerationResult:
        if not self.factors:
            return StateGenerationResult(self.states, ())
        return generate_state_space(self.factors, self.rules)

    def replay_states(self) -> tuple[VariantState, ...]:
        """Regenerate state identities while preserving stored allocation weights."""
        generated = self.preview_states().accepted
        weights = {state.state_id: state.weight for state in self.states}
        if set(weights) != {state.state_id for state in generated}:
            raise ValueError("Stored states do not match the factor and constraint definition")
        return tuple(replace(state, weight=weights[state.state_id]) for state in generated)
