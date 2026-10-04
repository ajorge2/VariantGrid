from __future__ import annotations

"""Dependency-light VariantGrid operator dashboard and sandbox.

The dashboard is intentionally a local demonstration surface. It makes the
product workflow and integrity gates executable without claiming that an
in-memory process is a production registry, event store, or authorization
boundary.
"""

from dataclasses import asdict, dataclass, replace
from collections import Counter
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
import argparse
import json
from math import isfinite
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .analysis import evaluate_binary_decision, minimum_detectable_effect, required_sample_size
from .api import APIRequest, APIResponse, VariantGridAPI
from .events import EventStore
from .models import Experiment, Factor, Guardrail, Rule, VariableType, generate_state_space
from .registry import (
    Lifecycle,
    RegisteredVersion,
    deserialize_experiment,
    serialize_experiment,
)
from .sdk import InProcessTransport, SDKConfig, VariantGridClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPROVED_PUBLIC_CLAIMS = ("VG-1", "VG-2R", "VG-3")
MAX_FACTORS = 100
MAX_PREVIEW_STATES = 100_000


class DashboardLifecycle(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    KILLED = "killed"


@dataclass(frozen=True)
class VariableDefinition:
    name: str
    variable_type: VariableType
    values: tuple[Any, ...]

    def factor(self) -> Factor:
        return Factor(self.name, self.values, self.variable_type)


@dataclass(frozen=True)
class ConstraintDefinition:
    when: Mapping[str, Any]
    require: Mapping[str, Any]
    reason: str
    effect: str = "require"
    match_order: str = "any_order"
    sequence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "when", MappingProxyType(dict(self.when)))
        object.__setattr__(self, "require", MappingProxyType(dict(self.require)))
        object.__setattr__(self, "sequence", tuple(self.sequence))

    def rule(self) -> Rule:
        return Rule(
            self.when,
            self.require,
            self.reason,
            self.effect,
            self.match_order,
            self.sequence,
        )

    def accepts(self, values: Mapping[str, Any]) -> bool:
        return self.rule().accepts(values)


@dataclass(frozen=True)
class DashboardDraft:
    key: str
    version: int
    hypothesis: str
    primary_metric: str
    guardrail_metric: str
    guardrail_maximum_regression: float | None
    variables: tuple[VariableDefinition, ...]
    constraints: tuple[ConstraintDefinition, ...]
    eligibility_rule: str
    control_allocation: float
    stopping_rule: str
    baseline_rate: float
    target_absolute_effect: float
    alpha: float = 0.05
    power: float = 0.80


@dataclass(frozen=True)
class PreviewState:
    values: Mapping[str, Any]
    accepted: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class StatePreview:
    states: tuple[PreviewState, ...]

    @property
    def valid(self) -> tuple[PreviewState, ...]:
        return tuple(state for state in self.states if state.accepted)

    @property
    def rejected(self) -> tuple[PreviewState, ...]:
        return tuple(state for state in self.states if not state.accepted)


@dataclass(frozen=True)
class DataHealth:
    stale_configuration: bool = False
    version_disagreement: bool = False
    missing_exposures: int = 0
    ingestion_delay_minutes: int = 2
    guardrail_breaches: tuple[str, ...] = ()
    expected_version: int = 1
    observed_version: int = 1


@dataclass(frozen=True)
class MetricFixture:
    control_exposed: int = 500
    control_converted: int = 50
    treatment_exposed: int = 500
    treatment_converted: int = 75
    expected_treatment_share: float = 0.5
    time_range: str = "2026-09-01 to 2026-09-14 UTC"
    unit: str = "activation rate"


@dataclass(frozen=True)
class LaunchedVersion:
    version: int
    configuration_json: str
    hypothesis: str
    lifecycle: DashboardLifecycle
    launched_at: str


@dataclass(frozen=True)
class DecisionSnapshot:
    version: int
    decision: str
    evidence_json: str
    created_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_draft(version: int = 1) -> DashboardDraft:
    return DashboardDraft(
        key="onboarding_optimization",
        version=version,
        hypothesis="A guided, template-first start increases activation without increasing support contacts.",
        primary_metric="activated_within_7_days",
        guardrail_metric="support_contacts_per_user",
        guardrail_maximum_regression=0.02,
        variables=(
            VariableDefinition("starter_type", VariableType.ENUM, ("template", "blank")),
            VariableDefinition("guided_tour", VariableType.BOOLEAN, (True, False)),
        ),
        constraints=(
            ConstraintDefinition(
                when={"starter_type": "blank"},
                require={"guided_tour": False},
                reason="Blank starts cannot show the template-guided tour.",
            ),
        ),
        eligibility_rule="new_accounts AND locale IN supported_locales",
        control_allocation=0.5,
        stopping_rule="Fixed horizon: 14 days and required sample size reached; no repeated peeking.",
        baseline_rate=0.10,
        target_absolute_effect=0.08,
    )


def preview_states(draft: DashboardDraft) -> StatePreview:
    if not draft.variables:
        return StatePreview(())
    names = [variable.name for variable in draft.variables]
    if len(names) != len(set(names)):
        return StatePreview(())
    state_count = 1
    for variable in draft.variables:
        state_count *= len(variable.values)
    if state_count > MAX_PREVIEW_STATES:
        return StatePreview(())
    states: list[PreviewState] = []
    try:
        for combination in product(*(variable.values for variable in draft.variables)):
            values = dict(zip(names, combination, strict=True))
            reasons = tuple(rule.reason for rule in draft.constraints if not rule.accepts(values))
            states.append(PreviewState(values, not reasons, reasons))
    except ValueError:
        return StatePreview(())
    return StatePreview(tuple(states))


def validate_draft(draft: DashboardDraft) -> tuple[str, ...]:
    errors: list[str] = []
    if not draft.key.strip():
        errors.append("Experiment key is required.")
    if not draft.hypothesis.strip():
        errors.append("Hypothesis is required.")
    if not draft.primary_metric.strip():
        errors.append("Primary metric is required.")
    if not draft.guardrail_metric.strip():
        errors.append("At least one guardrail metric is required.")
    if draft.guardrail_maximum_regression is None or not isfinite(draft.guardrail_maximum_regression):
        errors.append("Guardrail threshold is required.")
    elif draft.guardrail_maximum_regression < 0:
        errors.append("Guardrail threshold cannot be negative.")
    if not draft.eligibility_rule.strip():
        errors.append("Eligibility rule is required.")
    if not draft.stopping_rule.strip():
        errors.append("Stopping rule is required.")
    if not 0 < draft.control_allocation < 1:
        errors.append("Allocation must reserve traffic for control and treatment.")
    try:
        required_sample_size(
            baseline_rate=draft.baseline_rate,
            absolute_effect=draft.target_absolute_effect,
            alpha=draft.alpha,
            power=draft.power,
        )
    except ValueError as error:
        errors.append(f"Power inputs are invalid: {error}")
    if len(draft.variables) > MAX_FACTORS:
        errors.append(f"Experiments support at most {MAX_FACTORS} factors.")
    state_count = 1
    for variable in draft.variables:
        state_count *= max(1, len(variable.values))
    if state_count > MAX_PREVIEW_STATES:
        errors.append(
            f"The draft expands to {state_count:,} states; the dashboard preview limit is "
            f"{MAX_PREVIEW_STATES:,}."
        )
    try:
        factors = tuple(variable.factor() for variable in draft.variables)
        rules = tuple(constraint.rule() for constraint in draft.constraints)
        canonical_rules = [
            json.dumps(
                {
                    "when": dict(rule.when),
                    "require": dict(rule.require),
                    "effect": rule.effect,
                    "match_order": rule.match_order,
                    "sequence": list(rule.sequence),
                },
                sort_keys=True,
            )
            for rule in rules
        ]
        if len(canonical_rules) != len(set(canonical_rules)):
            errors.append("Rule configuration contains duplicate constraints.")
        if factors and state_count <= MAX_PREVIEW_STATES:
            generate_state_space(factors, rules)
    except ValueError as error:
        errors.append(f"Rule configuration is contradictory: {error}")
    preview = preview_states(draft)
    if not preview.states:
        errors.append("The state space is empty or variable names are invalid.")
    elif not preview.valid:
        errors.append("Constraints eliminated every possible state.")
    return tuple(errors)


def experiment_from_draft(draft: DashboardDraft) -> Experiment:
    sample_size = required_sample_size(
        baseline_rate=draft.baseline_rate,
        absolute_effect=draft.target_absolute_effect,
        alpha=draft.alpha,
        power=draft.power,
    )
    experiment = Experiment.from_factors(
        key=draft.key,
        version=draft.version,
        factors=tuple(variable.factor() for variable in draft.variables),
        primary_metric=draft.primary_metric,
        salt=f"{draft.key}-v{draft.version}",
        rules=tuple(constraint.rule() for constraint in draft.constraints),
        guardrails=(Guardrail(draft.guardrail_metric, draft.guardrail_maximum_regression or 0.0),),
        minimum_sample_size=sample_size,
        eligibility_rule=draft.eligibility_rule,
        stopping_rule=draft.stopping_rule,
    )
    if len(experiment.states) == 1:
        weights = (1.0,)
    else:
        treatment_weight = (1.0 - draft.control_allocation) / (len(experiment.states) - 1)
        weights = (draft.control_allocation,) + (treatment_weight,) * (len(experiment.states) - 1)
    return replace(
        experiment,
        states=tuple(
            replace(state, weight=weight)
            for state, weight in zip(experiment.states, weights, strict=True)
        ),
    )


class _SandboxRegistry:
    """Read-only registry view used by the SDK/API path in the dashboard."""

    def __init__(self, sandbox: "OperatorSandbox") -> None:
        self.sandbox = sandbox

    def get(self, key: str, version: int) -> RegisteredVersion:
        with self.sandbox.lock:
            launched = self.sandbox.versions.get(version)
            if launched is None:
                raise KeyError((key, version))
            experiment = deserialize_experiment(launched.configuration_json)
            if experiment.key != key:
                raise KeyError((key, version))
            lifecycle = {
                DashboardLifecycle.RUNNING: Lifecycle.RUNNING,
                DashboardLifecycle.PAUSED: Lifecycle.PAUSED,
                DashboardLifecycle.STOPPED: Lifecycle.STOPPED,
                DashboardLifecycle.KILLED: Lifecycle.STOPPED,
            }[launched.lifecycle]
            return RegisteredVersion(
                experiment=experiment,
                lifecycle=lifecycle,
                configuration_json=launched.configuration_json,
                launched_at=launched.launched_at,
            )


class OperatorSandbox:
    """Single-process workflow store used by the local operator demo."""

    def __init__(self, draft: DashboardDraft | None = None) -> None:
        self.lock = RLock()
        self.draft = draft or default_draft()
        self.versions: dict[int, LaunchedVersion] = {}
        self.deployed_version: int | None = None
        self.health = DataHealth()
        self.metrics = MetricFixture()
        self.decisions: list[DecisionSnapshot] = []
        self.messages: list[str] = []
        self.assignment_subject = "demo-user-42"
        self.event_store = EventStore()
        self.api = VariantGridAPI(_SandboxRegistry(self), self.event_store)

    @property
    def current_version(self) -> LaunchedVersion | None:
        if self.deployed_version is None:
            return None
        return self.versions[self.deployed_version]

    def update_draft(self, draft: DashboardDraft) -> None:
        if draft.version in self.versions:
            raise ValueError("Launched version contents are immutable; clone to a new version.")
        self.draft = draft

    def launch(self, *, review_confirmed: bool) -> LaunchedVersion:
        errors = list(validate_draft(self.draft))
        if not review_confirmed:
            errors.append("Confirm the immutable launch review before launching.")
        if self.draft.version in self.versions:
            errors.append("This version has already launched and cannot be overwritten.")
        if errors:
            raise ValueError(" | ".join(errors))
        experiment = experiment_from_draft(self.draft)
        launched = LaunchedVersion(
            version=self.draft.version,
            configuration_json=serialize_experiment(experiment),
            hypothesis=self.draft.hypothesis,
            lifecycle=DashboardLifecycle.RUNNING,
            launched_at=_utc_now(),
        )
        self.versions[launched.version] = launched
        self.deployed_version = launched.version
        self.health = replace(
            self.health,
            expected_version=launched.version,
            observed_version=launched.version,
            stale_configuration=False,
            version_disagreement=False,
        )
        self.metrics = replace(
            self.metrics,
            expected_treatment_share=1.0 - self.draft.control_allocation,
        )
        self.messages.append(f"Version {launched.version} launched from an immutable snapshot.")
        return launched

    def api_request(self, request: APIRequest) -> APIResponse:
        return self.api.handle(request)

    def live_readout(self, *, key: str | None = None, version: int | None = None) -> dict[str, Any]:
        with self.lock:
            current = self.current_version
            if current is None:
                return {
                    "status": "waiting_for_launch",
                    "generated_at": _utc_now(),
                    "event_totals": {},
                    "states": [],
                    "recent_events": [],
                }
            experiment = deserialize_experiment(current.configuration_json)
            requested_key = key or experiment.key
            requested_version = version or experiment.version
            if requested_key != experiment.key or requested_version not in self.versions:
                raise KeyError((requested_key, requested_version))
            launched = self.versions[requested_version]
            experiment = deserialize_experiment(launched.configuration_json)

        rows = self.event_store.export_events(experiment.key, experiment.version)
        totals = Counter(str(row["event_type"]) for row in rows)
        counts = self.event_store.binary_counts(
            experiment.key,
            experiment.version,
            experiment.primary_metric,
            attribution_window_hours=experiment.attribution_window_hours,
        )
        subjects_with_exposure = {
            str(row["subject_id"]) for row in rows if row["event_type"] == "exposure"
        }
        outcome_subjects = {
            str(row["subject_id"])
            for row in rows
            if row["event_type"] in {"goal", "observation", "guardrail"}
        }
        states = []
        for state in experiment.states:
            exposed, converted = counts.get(state.key, (0, 0))
            states.append(
                {
                    "state_key": state.key,
                    "state_id": state.state_id,
                    "values": dict(state.values),
                    "assigned": sum(
                        1
                        for row in rows
                        if row["event_type"] == "assignment" and row["variant_key"] == state.key
                    ),
                    "exposed": exposed,
                    "converted": converted,
                    "conversion_rate": converted / exposed if exposed else None,
                }
            )
        occurred = [str(row["occurred_at"]) for row in rows]
        recent = []
        for row in rows[-20:]:
            recent.append(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "subject_ref": sha256(str(row["subject_id"]).encode("utf-8")).hexdigest()[:10],
                    "variant_key": row["variant_key"],
                    "metric": row["metric"],
                    "value": row["value"],
                    "occurred_at": row["occurred_at"],
                }
            )
        return {
            "status": "live" if rows else "waiting_for_events",
            "experiment_key": experiment.key,
            "experiment_version": experiment.version,
            "lifecycle": launched.lifecycle.value,
            "primary_metric": experiment.primary_metric,
            "unit": "unique exposed subjects",
            "time_range": {
                "start": min(occurred) if occurred else None,
                "end": max(occurred) if occurred else None,
            },
            "generated_at": _utc_now(),
            "event_totals": {name: totals.get(name, 0) for name in sorted({"assignment", "exposure", "goal", "observation", "guardrail"})},
            "missing_exposure_subjects": len(outcome_subjects - subjects_with_exposure),
            "states": states,
            "recent_events": recent,
            "reconciliation_digest": self.event_store.reconcile(
                experiment.key,
                experiment.version,
                experiment.primary_metric,
                attribution_window_hours=experiment.attribution_window_hours,
            ).digest,
        }

    def run_reference_product(self, subject_id: str, *, convert: bool = False) -> dict[str, Any]:
        current = self.current_version
        if current is None or current.lifecycle is not DashboardLifecycle.RUNNING:
            raise ValueError("Launch a running experiment before opening the reference product.")
        experiment = deserialize_experiment(current.configuration_json)
        client = VariantGridClient(
            SDKConfig(experiment_versions={experiment.key: experiment.version}),
            InProcessTransport(self.api),
        )
        try:
            context = client.for_user(experiment.key, subject_id, eligible=True)
            values = {
                factor.name: context.get(factor.name, factor.values[0])
                for factor in experiment.factors
            }
            if convert:
                context.goal(
                    experiment.primary_metric,
                    idempotency_key=f"reference-goal:{experiment.key}:{experiment.version}:{subject_id}",
                )
            if not client.flush():
                raise RuntimeError("SDK events did not flush before the bounded timeout.")
            if client.delivery_failures:
                raise RuntimeError(str(client.delivery_failures[-1]))
            return {
                "subject_id": subject_id,
                "values": values,
                "state_key": context.debug_metadata.state_key,
                "version": experiment.version,
                "converted": convert,
            }
        finally:
            client.close()

    def clone_current(self) -> DashboardDraft:
        if self.current_version is None:
            raise ValueError("Launch a version before cloning.")
        new_version = max(self.versions) + 1
        self.draft = replace(self.draft, version=new_version)
        self.messages.append(f"Version {self.deployed_version} cloned to editable draft v{new_version}.")
        return self.draft

    def transition(self, action: str) -> LaunchedVersion:
        current = self.current_version
        if current is None:
            raise ValueError("No deployed version is available.")
        allowed = {
            DashboardLifecycle.RUNNING: {
                "pause": DashboardLifecycle.PAUSED,
                "stop": DashboardLifecycle.STOPPED,
                "kill": DashboardLifecycle.KILLED,
            },
            DashboardLifecycle.PAUSED: {
                "resume": DashboardLifecycle.RUNNING,
                "stop": DashboardLifecycle.STOPPED,
                "kill": DashboardLifecycle.KILLED,
            },
            DashboardLifecycle.STOPPED: {},
            DashboardLifecycle.KILLED: {},
        }
        if action not in allowed.get(current.lifecycle, {}):
            raise ValueError(f"Cannot {action} a {current.lifecycle.value} version.")
        updated = replace(current, lifecycle=allowed[current.lifecycle][action])
        self.versions[current.version] = updated
        self.messages.append(f"Version {current.version} is now {updated.lifecycle.value}; its configuration is unchanged.")
        return updated

    def rollback(self) -> LaunchedVersion:
        if self.deployed_version is None:
            raise ValueError("No deployed version is available.")
        prior = [version for version in self.versions if version < self.deployed_version]
        if not prior:
            raise ValueError("Rollback requires a previously launched version.")
        target_version = max(prior)
        self.deployed_version = target_version
        target = self.versions[target_version]
        if target.lifecycle in (DashboardLifecycle.STOPPED, DashboardLifecycle.KILLED):
            target = replace(target, lifecycle=DashboardLifecycle.RUNNING)
            self.versions[target_version] = target
        self.health = replace(
            self.health,
            expected_version=target_version,
            observed_version=target_version,
            version_disagreement=False,
        )
        self.messages.append(f"Deployment rolled back to immutable version {target_version}.")
        return target

    def inject_failure(self, failure: str) -> None:
        if failure == "clear":
            version = self.deployed_version or self.draft.version
            self.health = DataHealth(expected_version=version, observed_version=version)
        elif failure == "stale_configuration":
            self.health = replace(self.health, stale_configuration=True)
        elif failure == "version_disagreement":
            self.health = replace(
                self.health,
                version_disagreement=True,
                observed_version=max(0, self.health.expected_version - 1),
            )
        elif failure == "missing_exposure":
            self.health = replace(self.health, missing_exposures=37)
        elif failure == "ingestion_delay":
            self.health = replace(self.health, ingestion_delay_minutes=180)
        elif failure == "guardrail_breach":
            self.health = replace(self.health, guardrail_breaches=(self.draft.guardrail_metric,))
        elif failure == "srm":
            self.metrics = replace(self.metrics, control_exposed=900, treatment_exposed=100)
        elif failure == "underpowered":
            self.metrics = replace(self.metrics, control_exposed=25, treatment_exposed=25)
        else:
            raise ValueError(f"Unknown failure injection: {failure}")
        if failure == "clear":
            self.metrics = MetricFixture()
        self.messages.append(f"Failure fixture applied: {failure}.")

    def decision_readout(self):
        if self.current_version is None:
            return None
        experiment = experiment_from_draft_for_snapshot(self.current_version.configuration_json)
        return evaluate_binary_decision(
            control_exposed=self.metrics.control_exposed,
            control_converted=min(self.metrics.control_converted, self.metrics.control_exposed),
            treatment_exposed=self.metrics.treatment_exposed,
            treatment_converted=min(self.metrics.treatment_converted, self.metrics.treatment_exposed),
            minimum_sample_size_per_variant=experiment["minimum_sample_size"],
            expected_treatment_share=self.metrics.expected_treatment_share,
            guardrail_breaches=self.health.guardrail_breaches,
        )

    def integrity_failures(self) -> tuple[str, ...]:
        failures: list[str] = []
        if self.current_version is None:
            return ("experiment_not_launched",)
        if self.health.stale_configuration:
            failures.append("stale_configuration")
        if self.health.version_disagreement or self.health.expected_version != self.health.observed_version:
            failures.append("version_disagreement")
        if self.health.missing_exposures:
            failures.append("missing_exposure")
        if self.health.ingestion_delay_minutes > 60:
            failures.append("ingestion_delay")
        readout = self.decision_readout()
        if readout is not None:
            failures.extend(readout.integrity_failures)
        return tuple(dict.fromkeys(failures))

    def record_decision(self, decision: str) -> DecisionSnapshot:
        current = self.current_version
        if current is None:
            raise ValueError("Launch an experiment before recording a decision.")
        if current.lifecycle not in (DashboardLifecycle.RUNNING, DashboardLifecycle.PAUSED):
            raise ValueError("Decisions can only be recorded for running or paused versions.")
        failures = self.integrity_failures()
        if failures:
            raise ValueError("Decision blocked by integrity failures: " + ", ".join(failures))
        readout = self.decision_readout()
        assert readout is not None
        evidence = {
            "experiment_version": current.version,
            "immutable_configuration_json": current.configuration_json,
            "data_health": asdict(self.health),
            "metric_fixture": asdict(self.metrics),
            "readout": asdict(readout.readout),
            "recommendation": readout.recommendation,
            "operator_decision": decision,
        }
        snapshot = DecisionSnapshot(
            version=current.version,
            decision=decision,
            evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            created_at=_utc_now(),
        )
        self.decisions.append(snapshot)
        self.messages.append(f"Decision '{decision}' saved with an immutable evidence snapshot.")
        return snapshot


def experiment_from_draft_for_snapshot(configuration_json: str) -> dict[str, Any]:
    """Return frozen analysis inputs without depending on mutable dashboard state."""
    return json.loads(configuration_json)


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _status_badge(ok: bool, good: str, bad: str) -> str:
    label = good if ok else bad
    css = "good" if ok else "bad"
    return f'<span class="badge {css}">{escape(label)}</span>'


def load_public_claims(project_root: Path = PROJECT_ROOT) -> tuple[dict[str, Any], ...]:
    """Load only independently approved public claim manifests.

    The generated manifests remain the source of truth. A missing, malformed,
    or no-longer-approved manifest is returned as an explicit unavailable row
    rather than silently falling back to copied resume prose.
    """
    release_path = project_root / "artifacts" / "release_manifest.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return tuple(
            {
                "claim_id": claim_id,
                "status": "unavailable",
                "error": f"Generated release manifest unavailable: {error}",
            }
            for claim_id in APPROVED_PUBLIC_CLAIMS
        )
    release_fingerprint = release.get("source_fingerprint_sha256")
    release_claims = release.get("claims", {})
    claims: list[dict[str, Any]] = []
    for claim_id in APPROVED_PUBLIC_CLAIMS:
        path = project_root / "artifacts" / "claims" / f"{claim_id}.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            claims.append(
                {
                    "claim_id": claim_id,
                    "status": "unavailable",
                    "error": f"Generated manifest unavailable: {error}",
                }
            )
            continue
        if (
            manifest.get("claim_id") != claim_id
            or manifest.get("status") != "approved"
            or release_claims.get(claim_id) != "approved"
            or not release_fingerprint
            or manifest.get("source_fingerprint_sha256") != release_fingerprint
        ):
            claims.append(
                {
                    "claim_id": claim_id,
                    "status": "unavailable",
                    "error": "Generated result record and release metadata do not agree on status or provenance.",
                }
            )
            continue
        claims.append(manifest)
    return tuple(claims)


def _claim_by_id(claims: tuple[dict[str, Any], ...], claim_id: str) -> dict[str, Any]:
    return next((claim for claim in claims if claim.get("claim_id") == claim_id), {})


def _public_evidence_html(claims: tuple[dict[str, Any], ...]) -> str:
    unavailable = [claim for claim in claims if claim.get("status") != "approved"]
    if unavailable:
        rows = "".join(
            f"<li><strong>{escape(str(claim.get('claim_id', 'unknown')))}</strong>: "
            f"{escape(str(claim.get('error', 'evidence unavailable')))}</li>"
            for claim in unavailable
        )
        return (
            "<div class='withheld' role='alert'><strong>Validation results unavailable.</strong>"
            f"<ul>{rows}</ul>Results stay hidden until the generated records and release metadata agree.</div>"
        )

    vg1 = _claim_by_id(claims, "VG-1")
    vg2 = _claim_by_id(claims, "VG-2R")
    vg3 = _claim_by_id(claims, "VG-3")
    vg1_observed = vg1["observed"]
    vg2_observed = vg2["observed"]
    vg3_observed = vg3["observed"]
    environment = vg2["environment"]
    categories = vg3_observed["event_categories"]
    vg3_thresholds = vg3["predeclared_thresholds"]
    false_positive_bounds = vg3_thresholds["false_positive_rate_percent"]
    coverage_bounds = vg3_thresholds["confidence_interval_coverage_percent"]
    category_list = ", ".join(name.replace("_", "-") for name in categories)
    policy_count_label = (
        "three" if len(vg2_observed["policies"]) == 3 else str(len(vg2_observed["policies"]))
    )
    trial_count_label = (
        "five" if vg2_observed["trial_count"] == 5 else str(vg2_observed["trial_count"])
    )
    system_results = (
        {
            "claim_id": "VG-1",
            "title": "Experiment workflow",
            "summary": (
                "Typed states · constraints · versioned assignment · retry-safe exposure logging"
            ),
            "live_links": (
                ("Build a test", "#operator-workflow"),
                ("Try assignment", "#assignment-lab"),
                ("Run safety tests", "#event-integrity-lab"),
            ),
            "detail": (
                f"{len(vg1_observed['required_tests_present'])} regression tests cover the connected workflow."
            ),
        },
        {
            "claim_id": "VG-2R",
            "title": "Assignment consistency",
            "summary": (
                f"100% sticky · {vg2_observed['assignments_per_policy'] * len(vg2_observed['policies']) // 1_000_000}M "
                f"simulated assignments · {vg2_observed['maximum_drift_percentage_points']:.4f} pp max drift"
            ),
            "live_links": (("Try assignment", "#assignment-lab"),),
            "detail": (
                f"{policy_count_label.title()} allocation policies with "
                f"{vg2_observed['assignments_per_policy']:,} assignments each; "
                f"{vg2_observed['median_trial_sampled_p95_microseconds']:.3f} µs median sampled p95 "
                f"across {trial_count_label} local trials."
            ),
        },
        {
            "claim_id": "VG-3",
            "title": "Inference and event integrity",
            "summary": (
                f"{vg3_observed['false_positive_rate_percent']:.2f}% false positives · "
                f"{vg3_observed['interval_coverage_percent']:.2f}% interval coverage · "
                f"{len(categories)}/{len(categories)} safety scenarios passed"
            ),
            "live_links": (("Run safety tests", "#event-integrity-lab"),),
            "detail": (
                f"{vg3_observed['simulations']:,} fixed-seed A/A simulations plus duplicate, "
                "missing, late, reordered, and cross-version event scenarios."
            ),
        },
    )
    result_rows = "".join(
        f"<details class='result-item'><summary><span class='result-copy'>"
        f"<strong class='result-title'>{escape(row['title'])}</strong>"
        f"<span class='result-summary'>{escape(row['summary'])}</span></span>"
        f"<span class='disclosure' aria-hidden='true'></span></summary><div class='result-body'>"
        f"<p class='result-detail'>{escape(row['detail'])}</p>"
        f"<p class='detail-links'><strong>Explore:</strong> "
        + " · ".join(
            f"<a href='{escape(href)}'>{escape(label)}</a>" for label, href in row["live_links"]
        )
        + f"<br><strong>Generated record:</strong> <code>{escape(row['claim_id'])}.json</code>"
        f"</div></details>"
        for row in system_results
    )
    artifact_rows = "".join(
        f"<li><code>{escape(path)}</code></li>" for path in vg2["generated_artifacts"]
    )
    return f"""
    <div class="result-list" aria-label="System validation results">{result_rows}</div>
    <details class="technical-details"><summary>Methodology</summary>
      <div class="split"><div>
        <h3>Reproducibility</h3><ul>{artifact_rows}</ul>
        <p><strong>Run locally:</strong> <code>{escape(vg2['reproduction_command'])}</code></p>
        <p><strong>Source fingerprint:</strong> <code>{escape(vg2['source_fingerprint_sha256'])}</code></p>
      </div><div>
        <h3>Measured scope</h3>
        <p>Assignment evidence uses deterministic local simulation for 50/50, 80/20, and 90/10 policies. Latency is an in-process Python microbenchmark sampled every 100 calls on {escape(str(environment.get('cpu_model') or 'CPU model not reported'))} / {escape(str(environment.get('python_implementation') or 'Python implementation not reported'))} {escape(str(environment.get('python') or 'version not reported'))}; it is not network latency.</p>
        <p>Calibration evidence uses frozen seeds and synthetic A/A ground truth. Event integrity uses local SQLite tests. These results do not establish production durability, Postgres behavior, uptime, customer adoption, causal business impact, or revenue lift.</p>
        <p>Calibration thresholds: {false_positive_bounds[0]:.1f}–{false_positive_bounds[1]:.1f}% false positives and {coverage_bounds[0]:.1f}–{coverage_bounds[1]:.1f}% coverage. Integrity categories: {escape(category_list)}.</p>
      </div></div>
    </details>
    """


def _assignment_lab_html(sandbox: OperatorSandbox, project_root: Path = PROJECT_ROOT) -> str:
    """Render one real deterministic assignment plus signed benchmark evidence."""
    try:
        from .assignment_lab import AssignmentLab, AssignmentEvidenceError

        # The lab explicitly models a qualifying subject. Production callers
        # must evaluate the frozen eligibility expression against trusted data.
        current = replace(experiment_from_draft(sandbox.draft), eligibility_rule="all")
        # Hold every other input fixed so this specifically demonstrates that
        # changing the immutable version changes the assignment namespace.
        comparison = replace(current, version=current.version + 1)
        snapshot = AssignmentLab(current, comparison, project_root).snapshot(
            sandbox.assignment_subject
        )
    except (AssignmentEvidenceError, OSError, ValueError) as error:
        return (
            "<div class='withheld' role='alert'><strong>Assignment evidence unavailable.</strong> "
            f"{escape(str(error))}</div>"
        )

    same = snapshot["same_version"]
    first = same["first"]
    isolation = snapshot["version_isolation"]
    comparison_record = isolation["comparison"]
    benchmark = snapshot["benchmark_evidence"]
    policies = "".join(
        f"<tr><td>{escape(row['policy'].replace('_', '/'))}</td>"
        f"<td>{row['assignments']:,}</td><td>{row['stickiness_percent']:.0f}%</td>"
        f"<td>{row['maximum_drift_percentage_points']:.4f} pp</td>"
        f"<td><code>{escape(_json_text(row['allocation_counts']))}</code></td></tr>"
        for row in benchmark["allocation_policies"]
    )
    latency_trials = "".join(
        f"<tr><td>{row['trial']}</td><td>{row['assignments']:,}</td>"
        f"<td>{row['sample_every']:,}</td><td>{row['sampled_p50_microseconds']:.3f} µs</td>"
        f"<td>{row['sampled_p95_microseconds']:.3f} µs</td>"
        f"<td>{row['sampled_p99_microseconds']:.3f} µs</td>"
        f"<td>{row['throughput_assignments_per_second']:,.0f}/s</td></tr>"
        for row in benchmark["latency"]["trials"]
    )
    interval = first["interval"]
    bucket = "not evaluated" if first["bucket"] is None else f"{first['bucket']:.8f}"
    assignment_status = _status_badge(
        bool(same["identical"]), "PASS · repeat assignment identical", "FAIL · repeat changed"
    )
    compact_assignment_status = _status_badge(
        bool(same["identical"]), "Same twice", "Changed"
    )
    isolation_status = _status_badge(
        bool(isolation["isolated"]), "PASS · version namespace isolated", "FAIL · namespace reused"
    )
    return f"""
    <form method="post" action="/assignment-lab" class="inline-form">
      <label>User ID<input name="subject_id" maxlength="512" value="{escape(sandbox.assignment_subject)}"></label>
      <button type="submit">Assign</button>
    </form>
    <article class="assignment-result">
      <div><h3>{escape(first['state_key'] or 'No state')}</h3></div>
      <div>{compact_assignment_status}</div>
      <dl><dt>Probability</dt><dd>{first['probability']:.1%}</dd><dt>Version</dt><dd>v{first['version']}</dd></dl>
    </article>
    <details class="technical-details"><summary>Why it repeats</summary>
      <p>{assignment_status}</p><div class="split"><div><h3>Assignment inputs</h3>
        <dl><dt>Typed values</dt><dd><code>{escape(_json_text(first['values']))}</code></dd>
        <dt>Policy</dt><dd>{escape(first['algorithm_version'])} · {escape(first['policy_version'])}</dd>
        <dt>Hash bucket</dt><dd>{escape(bucket)} in [{interval['start']:.6f}, {interval['end']:.6f})</dd></dl>
      </div><div><h3>Version isolation</h3><p>{isolation_status}</p>
        <p>v{first['version']} → v{comparison_record['version']} · bucket <code>{comparison_record['bucket']:.8f}</code> · state <strong>{escape(comparison_record['state_key'] or 'none')}</strong></p>
        <p class="muted">{escape(isolation['interpretation'])}</p></div></div>
    </details>
    <details class="technical-details"><summary>Benchmark</summary>
      <table><thead><tr><th>Policy</th><th>Assignments</th><th>Repeat stickiness</th><th>Max drift</th><th>Observed counts</th></tr></thead>
      <tbody>{policies}</tbody></table>
      <p class="evidence-note"><strong>Latency:</strong> {benchmark['latency']['median_sampled_p95_microseconds']:.3f} µs median sampled p95 across {benchmark['latency']['trial_count']} local in-process trials; range {benchmark['latency']['minimum_sampled_p95_microseconds']:.3f}–{benchmark['latency']['maximum_sampled_p95_microseconds']:.3f} µs. This is hardware- and load-sensitive microbenchmark evidence, not API latency.</p>
      <details><summary>Inspect all latency trials</summary>
        <table><thead><tr><th>Trial</th><th>Assignments</th><th>Sample every</th><th>Sampled p50</th><th>Sampled p95</th><th>Sampled p99</th><th>Throughput</th></tr></thead>
        <tbody>{latency_trials}</tbody></table>
      </details>
    </details>
    """


def _event_integrity_html() -> str:
    """Run protected scenarios through the real local ingestion pipeline."""
    try:
        from .integrity_lab import run_event_integrity_lab

        report = run_event_integrity_lab().to_dict()
    except (OSError, RuntimeError, ValueError) as error:
        return (
            "<div class='withheld' role='alert'><strong>Integrity lab unavailable.</strong> "
            f"{escape(str(error))}</div>"
        )
    rows = "".join(
        f"<details class='integrity-row'><summary><strong>{escape(str(row['scenario']).replace('_', ' ').title())}</strong>"
        f"{'' if row['passed'] else _status_badge(False, 'PASS', 'FAIL')}"
        f"<span class='row-chevron' aria-hidden='true'></span></summary>"
        f"<div class='integrity-body'><p>{escape(str(row['expected_invariant']))}</p>"
        f"<p><strong>Test:</strong> {escape(str(row['fixture']))}</p>"
        f"<details><summary>Observed pipeline result</summary><pre>{escape(_json_text(row['observed']))}</pre></details>"
        f"<p><strong>Reproduce:</strong> <code>{escape(str(row['reproduction_command']))}</code></p></div></details>"
        for row in report["rows"]
    )
    summary = report["summary"]
    return f"""
    <div class="integrity-summary"><div>{_status_badge(bool(summary['all_passed']), f"{summary['passed']}/{summary['total']} passed", f"{summary['failed']} failed")}</div></div>
    <div class="integrity-list">{rows}</div>
    <details class="technical-details"><summary>Scope</summary><p>{escape(str(report['scope_note']))}</p></details>
    """


def render_dashboard(
    sandbox: OperatorSandbox, project_root: Path = PROJECT_ROOT
) -> str:
    draft = sandbox.draft
    preview = preview_states(draft)
    errors = validate_draft(draft)
    launched = sandbox.current_version
    required_n: int | None = None
    mde: float | None = None
    try:
        required_n = required_sample_size(
            baseline_rate=draft.baseline_rate,
            absolute_effect=draft.target_absolute_effect,
            alpha=draft.alpha,
            power=draft.power,
        )
        mde = minimum_detectable_effect(
            baseline_rate=draft.baseline_rate,
            subjects_per_variant=required_n,
            alpha=draft.alpha,
            power=draft.power,
        )
    except ValueError:
        pass

    health_failures = sandbox.integrity_failures()
    readout = sandbox.decision_readout()
    launched_configuration = (
        experiment_from_draft_for_snapshot(launched.configuration_json) if launched else None
    )
    variables_json = _json_text(
        [
            {"name": item.name, "type": item.variable_type.value, "values": list(item.values)}
            for item in draft.variables
        ]
    )
    constraints_json = _json_text(
        [
            {
                "when": dict(item.when),
                "require": dict(item.require),
                "reason": item.reason,
                "effect": item.effect,
                "match_order": item.match_order,
                "sequence": list(item.sequence),
            }
            for item in draft.constraints
        ]
    )
    messages = "".join(f"<li>{escape(message)}</li>" for message in sandbox.messages[-4:])
    error_html = "".join(f"<li>{escape(error)}</li>" for error in errors)
    valid_rows_parts: list[str] = []
    treatment_share = (
        (1.0 - draft.control_allocation) / (len(preview.valid) - 1)
        if len(preview.valid) > 1
        else 0.0
    )
    for index, state in enumerate(preview.valid):
        allocation = 1.0 if len(preview.valid) == 1 else (draft.control_allocation if index == 0 else treatment_share)
        role = "Control" if index == 0 else "Treatment"
        valid_rows_parts.append(
            f"<tr><td>Valid · {role}</td><td><code>{escape(_json_text(dict(state.values)))}</code></td>"
            f"<td>All constraints passed · allocation {allocation:.1%}</td></tr>"
        )
    valid_rows = "".join(valid_rows_parts)
    rejected_rows = "".join(
        f"<tr class='rejected'><td>Rejected</td><td><code>{escape(_json_text(dict(state.values)))}</code></td>"
        f"<td>{escape('; '.join(state.reasons))}</td></tr>"
        for state in preview.rejected
    )
    version_rows = "".join(
        f"<tr><td>v{version.version}</td><td>{escape(version.lifecycle.value)}</td>"
        f"<td>{escape(version.launched_at)}</td><td><code>{escape(version.configuration_json[:72])}…</code></td></tr>"
        for version in sorted(sandbox.versions.values(), key=lambda item: item.version, reverse=True)
    ) or "<tr><td colspan='4'>No launched versions yet.</td></tr>"
    decision_rows = "".join(
        f"<tr><td>v{item.version}</td><td>{escape(item.decision)}</td><td>{escape(item.created_at)}</td>"
        f"<td><code>{escape(item.evidence_json[:72])}…</code></td></tr>"
        for item in reversed(sandbox.decisions)
    ) or "<tr><td colspan='4'>No decisions recorded.</td></tr>"

    health_items = [
        (not sandbox.health.stale_configuration, "Configuration current", "Stale SDK configuration"),
        (not sandbox.health.version_disagreement, "Versions agree", "Version disagreement"),
        (sandbox.health.missing_exposures == 0, "Exposure complete", f"{sandbox.health.missing_exposures} outcomes lack exposure"),
        (sandbox.health.ingestion_delay_minutes <= 60, f"Ingestion delay {sandbox.health.ingestion_delay_minutes}m", f"Ingestion delayed {sandbox.health.ingestion_delay_minutes}m"),
        (not sandbox.health.guardrail_breaches, "Guardrails healthy", "Guardrail breach"),
    ]
    if readout is not None:
        health_items.append(
            (
                readout.readout.sample_ratio_mismatch_p_value >= 0.001,
                f"Allocation healthy (SRM p={readout.readout.sample_ratio_mismatch_p_value:.3g})",
                f"Sample-ratio mismatch (p={readout.readout.sample_ratio_mismatch_p_value:.3g})",
            )
        )
        required = launched_configuration["minimum_sample_size"] if launched_configuration else 0
        health_items.append(
            (
                min(sandbox.metrics.control_exposed, sandbox.metrics.treatment_exposed) >= required,
                f"Power threshold reached (n≥{required:,}/state)",
                f"Underpowered (requires n≥{required:,}/state)",
            )
        )
    health_html = "".join(f"<li>{_status_badge(ok, good, bad)}</li>" for ok, good, bad in health_items)

    if launched is None:
        effects_html = "<p class='withheld'>Launch a reviewed version before viewing effects.</p>"
    elif health_failures:
        effects_html = (
            "<div class='withheld' role='alert'><strong>Effect estimates withheld.</strong> "
            "Resolve integrity failures before interpreting results or recording a decision. "
            f"Blocked by: {escape(', '.join(health_failures))}.</div>"
        )
    else:
        assert readout is not None
        control_width = max(1, round(readout.readout.control_rate * 500))
        treatment_width = max(1, round(readout.readout.treatment_rate * 500))
        effects_html = f"""
        <figure aria-label="Primary metric comparison for experiment version {launched.version}">
          <figcaption><strong>{escape(launched_configuration['primary_metric'])}</strong> · unit: {escape(sandbox.metrics.unit)} ·
          range: {escape(sandbox.metrics.time_range)} · version: {launched.version}</figcaption>
          <div class="bar-row"><span>Control {readout.readout.control_rate:.1%} (n={sandbox.metrics.control_exposed:,})</span><i style="width:{control_width}px"></i></div>
          <div class="bar-row"><span>Treatment {readout.readout.treatment_rate:.1%} (n={sandbox.metrics.treatment_exposed:,})</span><i style="width:{treatment_width}px"></i></div>
          <p>Absolute effect {readout.readout.absolute_lift:+.2%}; 95% CI
          [{readout.readout.confidence_interval_95[0]:+.2%}, {readout.readout.confidence_interval_95[1]:+.2%}];
          p={readout.readout.p_value:.4f}. Recommendation: <strong>{escape(readout.recommendation or 'none')}</strong>.</p>
        </figure>
        """

    if launched is None:
        live_html = (
            "<div class='live-panel'><h3>Live SDK telemetry</h3>"
            "<p class='muted'>Launch a version to accept assignment, exposure, and outcome events.</p></div>"
        )
        live_script = ""
    else:
        live_html = f"""
        <div class="live-panel" data-live-key="{escape(draft.key)}" data-live-version="{launched.version}">
          <div class="live-head"><div><span class="eyebrow">Event-derived readout</span><h3>Live SDK telemetry</h3></div>
          <a class="product-link" href="/reference-product" target="_blank" rel="noopener">Open instrumented product ↗</a></div>
          <p id="live-status" class="muted" aria-live="polite">Connecting to the event stream…</p>
          <div id="live-totals" class="telemetry-grid" aria-label="Live event totals"></div>
          <div class="split"><figure><figcaption><strong>Traffic by state</strong> · unit: unique subjects · version: {launched.version}</figcaption><div id="live-traffic"></div></figure>
          <figure><figcaption><strong>{escape(draft.primary_metric)}</strong> · unit: conversion rate · version: {launched.version}</figcaption><div id="live-conversion"></div></figure></div>
          <details class="technical-details"><summary>Recent event trace</summary><div class="event-scroll"><table><thead><tr><th>Time</th><th>Type</th><th>Subject ref</th><th>State / metric</th></tr></thead><tbody id="live-events"></tbody></table></div><p id="live-digest" class="muted"></p></details>
        </div>
        """
        live_script = """
<script>
(() => {
  const panel = document.querySelector('[data-live-key]');
  if (!panel) return;
  const status = document.getElementById('live-status');
  const totals = document.getElementById('live-totals');
  const traffic = document.getElementById('live-traffic');
  const conversion = document.getElementById('live-conversion');
  const events = document.getElementById('live-events');
  const digest = document.getElementById('live-digest');
  const row = (label, value, maximum, suffix='') => {
    const outer = document.createElement('div'); outer.className = 'live-bar-row';
    const text = document.createElement('span'); text.textContent = `${label} ${value}${suffix}`;
    const track = document.createElement('b'); const fill = document.createElement('i');
    fill.style.width = `${maximum ? Math.max(2, value / maximum * 100) : 0}%`;
    track.appendChild(fill); outer.append(text, track); return outer;
  };
  async function refresh() {
    try {
      const query = new URLSearchParams({key: panel.dataset.liveKey, version: panel.dataset.liveVersion});
      const response = await fetch(`/api/live-readout?${query}`, {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const start = data.time_range.start ? new Date(data.time_range.start).toLocaleTimeString() : 'no events';
      const end = data.time_range.end ? new Date(data.time_range.end).toLocaleTimeString() : 'now';
      status.textContent = data.status === 'waiting_for_events'
        ? 'Connected · waiting for the first SDK event.'
        : `Live · ${start} to ${end} · ${data.missing_exposure_subjects} outcome subjects missing exposure`;
      totals.replaceChildren(...Object.entries(data.event_totals).map(([name, count]) => {
        const card = document.createElement('div'); const value = document.createElement('strong');
        const label = document.createElement('span'); value.textContent = count; label.textContent = name;
        card.append(value, label); return card;
      }));
      const maxTraffic = Math.max(0, ...data.states.map(item => Math.max(item.assigned, item.exposed)));
      traffic.replaceChildren(...data.states.map(item => row(item.state_key, item.exposed, maxTraffic, ' exposed')));
      conversion.replaceChildren(...data.states.map(item => row(item.state_key, Math.round((item.conversion_rate || 0) * 1000) / 10, 100, `% (n=${item.exposed})`)));
      events.replaceChildren(...data.recent_events.slice().reverse().map(item => {
        const tr = document.createElement('tr');
        [item.occurred_at, item.event_type, item.subject_ref, item.variant_key || item.metric || '—'].forEach(value => {
          const td = document.createElement('td'); td.textContent = value; tr.appendChild(td);
        }); return tr;
      }));
      digest.textContent = `Reconciliation digest ${data.reconciliation_digest}`;
    } catch (error) { status.textContent = `Live telemetry unavailable: ${error.message}`; }
  }
  refresh(); window.setInterval(refresh, 2000);
})();
</script>
"""

    lifecycle = launched.lifecycle.value if launched else "not launched"
    health_step_state = (
        ""
        if launched is None
        else (
            "healthy"
            if not health_failures
            else f"{len(health_failures)} failure"
            + ("s" if len(health_failures) != 1 else "")
        )
    )
    config_step_state = f"{len(errors)} issues" if errors else ""
    readout_step_state = (
        escape(readout.recommendation)
        if launched is not None and readout and readout.recommendation
        else ""
    )
    decision_step_state = f"{len(sandbox.decisions)} saved" if sandbox.decisions else ""
    lifecycle_step_state = escape(lifecycle) if launched is not None else ""

    def step_state(value: str) -> str:
        return f'<span class="step-state">{value}</span>' if value else ""

    decision_lifecycle_ok = launched is not None and launched.lifecycle in (
        DashboardLifecycle.RUNNING,
        DashboardLifecycle.PAUSED,
    )
    disabled = " disabled" if health_failures or not decision_lifecycle_ok else ""
    assignment_lab_html = _assignment_lab_html(sandbox, project_root)
    event_integrity_html = _event_integrity_html()
    validation_evidence_html = _public_evidence_html(load_public_claims(project_root))
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VariantGrid experimentation system</title>
<style>
:root{{--ink:#17191c;--muted:#687078;--surface:#fff;--subtle:#f6f7f8;--line:#e3e6e8;--accent:#2563eb;--good:#18794e;--bad:#b42318;--bg:#f4f5f6}}
*{{box-sizing:border-box}} html{{scroll-behavior:smooth}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}}
main,.shell-header,.nav-shell{{max-width:960px;margin:auto}} main{{padding:0 24px 72px}} .shell-header{{padding:40px 24px 0}}
h1,h2,h3,p{{margin-top:0}} h1{{font-size:42px;line-height:1.08;letter-spacing:-.035em;max-width:720px;margin:48px 0 14px}} h2{{font-size:24px;letter-spacing:-.02em;margin-bottom:6px}} h3{{font-size:17px;margin-bottom:8px}}
.brand-row{{display:flex;align-items:center;gap:16px}} .brand{{font-weight:800;letter-spacing:-.02em}} .brand-mark{{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:7px;background:var(--ink);color:#fff;font-size:12px;margin-right:8px}}
.lede,.muted,small{{color:var(--muted)}} .lede{{font-size:18px;max-width:700px;margin-bottom:24px}}
.nav-shell{{padding:0 24px;position:sticky;top:12px;z-index:5}} .section-nav{{display:flex;gap:4px;flex-wrap:wrap;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:4px;box-shadow:0 6px 22px rgba(17,24,39,.06)}}
.section-nav a{{color:var(--muted);padding:8px 12px;border-radius:8px;text-decoration:none;font-weight:650}} .section-nav a:hover{{background:var(--subtle);color:var(--ink)}}
section{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:28px;margin:20px 0;scroll-margin-top:78px}}
.section-heading,.section-intro{{display:flex;align-items:flex-start;justify-content:space-between;gap:28px;margin-bottom:18px}} .section-heading p,.section-intro p{{max-width:380px;margin:2px 0 0;color:var(--muted)}}
.eyebrow{{display:block;color:var(--accent);font-size:11px;font-weight:800;letter-spacing:.09em;text-transform:uppercase}}
.grid,.split{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}}
label{{display:block;font-weight:650;margin:12px 0 5px}} input,textarea,select{{width:100%;background:#fff;color:var(--ink);border:1px solid #cfd4d8;border-radius:9px;padding:10px 11px;font:inherit}}
input:focus,textarea:focus,select:focus{{outline:3px solid #dbeafe;border-color:var(--accent)}} textarea{{min-height:82px;resize:vertical}}
button{{background:var(--ink);color:#fff;border:0;border-radius:9px;padding:10px 14px;font-weight:750;cursor:pointer}} button:hover{{background:#30343a}} button.secondary{{background:#eef0f2;color:var(--ink)}} button.danger{{background:#fee4e2;color:var(--bad)}} button:disabled{{opacity:.38;cursor:not-allowed}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{text-align:left;border-bottom:1px solid var(--line);padding:10px 8px;vertical-align:top}} th{{color:var(--muted);font-weight:650}} code,pre{{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:#344054;white-space:pre-wrap;overflow-wrap:anywhere}}
.badge{{display:inline-block;border-radius:999px;padding:4px 8px;font-size:12px;font-weight:800}} .good{{background:#e7f6ee;color:var(--good)}} .bad{{background:#feeceb;color:var(--bad)}}
.withheld{{border:1px solid #f7b4ae;background:#fff6f5;padding:14px;border-radius:10px;color:#7a271a}} .review{{border-left:2px solid var(--line);padding-left:16px}}
.rejected{{color:var(--bad)}} .bar-row{{display:flex;align-items:center;gap:10px;margin:11px 0}} .bar-row span{{width:210px}} .bar-row i{{height:12px;background:var(--accent);display:inline-block;border-radius:999px}}
.messages{{list-style:none;padding:0;margin:14px 0}} .messages:empty{{display:none}} .messages li{{background:#eef4ff;border-radius:9px;padding:10px 12px;color:#254f9c}}
.result-list{{border-top:1px solid var(--line)}} .result-item{{margin:0;border-bottom:1px solid var(--line)}} .result-item>summary{{display:grid;grid-template-columns:1fr auto;gap:14px;align-items:center;padding:18px 0;list-style:none;cursor:pointer}}
.result-item>summary::-webkit-details-marker{{display:none}} .result-copy{{display:block}} .result-title,.result-summary{{display:block}} .result-title{{font-weight:750;line-height:1.35}} .result-summary{{color:var(--muted);margin-top:3px}} .disclosure{{font-size:12px;color:var(--muted)}} .result-body{{padding:0 0 18px}}
.disclosure::after,.row-chevron::after{{content:'›';font-size:20px;color:#98a0a8}}
.result-detail{{font-weight:650}} .detail-links{{margin:0;color:var(--muted)}} .detail-links strong{{color:var(--ink)}} .detail-links a{{color:var(--accent);font-weight:700}}
details.technical-details{{border-top:1px solid var(--line);padding:14px 0;margin-top:12px}} details.technical-details>summary{{cursor:pointer;font-weight:750;color:var(--muted)}} details.technical-details[open]>summary{{color:var(--ink);margin-bottom:16px}}
.inline-form{{display:flex;align-items:end;gap:10px;max-width:620px;margin:12px 0 20px}} .inline-form label{{flex:1;margin:0}} .assignment-result{{display:flex;align-items:center;justify-content:space-between;gap:20px;background:var(--subtle);border-radius:14px;padding:18px}} .assignment-result>*{{min-width:0}}
.assignment-result h3{{font-size:20px;margin:3px 0 0;overflow-wrap:anywhere}} .assignment-result dl{{display:flex;gap:18px;margin:0}} dl{{display:grid;grid-template-columns:max-content 1fr;gap:6px 12px}} dt{{color:var(--muted)}} dd{{margin:0}}
.evidence-note{{border-left:2px solid var(--line);padding-left:12px;color:var(--muted)}} .integrity-summary{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:8px}} .integrity-summary p{{margin:0}}
.integrity-row{{border-top:1px solid var(--line);margin:0}} .integrity-row>summary{{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:14px 0;cursor:pointer;list-style:none}} .integrity-row>summary::-webkit-details-marker{{display:none}} .integrity-row small{{display:block;max-width:680px;margin-top:3px}} .integrity-body{{padding:0 0 14px}}
.workflow-shell{{padding:0;overflow:hidden}} .workflow-head{{padding:28px 28px 18px}} .workflow-step{{border-top:1px solid var(--line);margin:0}} .workflow-step>summary{{display:grid;grid-template-columns:30px 1fr auto;gap:14px;align-items:center;padding:18px 28px;cursor:pointer;list-style:none}} .workflow-step>summary::-webkit-details-marker{{display:none}}
.step-number{{display:grid;place-items:center;width:28px;height:28px;border:1px solid var(--line);border-radius:8px;color:var(--muted);font-size:12px;font-weight:800}} .step-title strong,.step-title small{{display:block}} .step-body{{padding:0 28px 26px}} .step-state{{font-size:12px;color:var(--muted)}}
.actions form{{display:flex;flex-wrap:wrap;gap:8px}} footer{{padding:12px 2px}} pre{{max-width:100%;overflow:auto}}
.live-panel{{border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:22px;overflow:hidden}} .live-head{{display:flex;align-items:start;justify-content:space-between;gap:18px}} .live-head>*,.split>*{{min-width:0}} .live-head h3{{margin:3px 0 0}} .product-link{{color:var(--accent);font-weight:750;text-align:right;overflow-wrap:anywhere}} .live-panel figcaption{{overflow-wrap:anywhere}}
.telemetry-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:14px 0 20px}} .telemetry-grid div{{background:var(--subtle);border-radius:10px;padding:10px}} .telemetry-grid strong,.telemetry-grid span{{display:block}} .telemetry-grid strong{{font-size:20px}} .telemetry-grid span{{color:var(--muted);font-size:12px;text-transform:capitalize}}
.live-bar-row{{margin:10px 0}} .live-bar-row span{{display:block;font-size:12px;overflow-wrap:anywhere}} .live-bar-row b{{display:block;height:9px;background:#eef0f2;border-radius:99px;margin-top:4px;overflow:hidden}} .live-bar-row i{{display:block;height:100%;background:var(--accent);border-radius:99px}} .event-scroll{{max-height:260px;overflow:auto}}
@media(max-width:720px){{.shell-header{{padding:24px 14px 0}} .nav-shell{{padding:0 14px}} main{{padding:0 14px 48px}} h1{{font-size:34px;margin-top:34px}} .section-nav{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}} .section-nav a{{text-align:center}} section{{padding:21px;scroll-margin-top:118px}} .section-heading,.section-intro,.grid,.split{{display:block}} .section-heading p,.section-intro p{{margin-top:8px}} .inline-form,.assignment-result,.integrity-summary,.live-head{{display:block}} .telemetry-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}} .inline-form button{{margin-top:10px;width:100%}} .assignment-result dl{{margin-top:14px}} .result-item>summary{{grid-template-columns:1fr auto}} .workflow-step>summary{{padding:16px 20px;grid-template-columns:30px 1fr}} .step-state{{display:none}} .step-body{{padding:0 20px 22px}} table{{display:block;overflow-x:auto}}}}
</style>
</head>
<body><header class="shell-header">
<div class="brand-row"><div class="brand"><span class="brand-mark">V</span>VariantGrid</div></div>
<h1>Trust an experiment before acting on it.</h1>
</header><div class="nav-shell"><nav class="section-nav" aria-label="Dashboard sections"><a href="#validation-evidence">Overview</a><a href="#assignment-lab">Assignment</a><a href="#event-integrity-lab">Safety tests</a><a href="#operator-workflow">Build a test</a></nav></div><main>
<ul class="messages" aria-live="polite">{messages}</ul>

<section id="validation-evidence"><div class="section-heading"><h2>System snapshot</h2></div>{validation_evidence_html}</section>

<section id="assignment-lab"><div class="section-heading"><h2>Assignment</h2><p>Same user + version = same state.</p></div>{assignment_lab_html}</section>

<section id="event-integrity-lab"><div class="section-heading"><h2>Event integrity</h2></div>{event_integrity_html}</section>

<section id="operator-workflow" class="workflow-shell"><div class="workflow-head"><h2>Build and review a test</h2></div>

<details id="configuration" class="workflow-step" open><summary><span class="step-number">1</span><span class="step-title"><strong>Define the decision</strong></span>{step_state(config_step_state)}</summary><div class="step-body">
<form method="post" action="/preview">
<label>Test name<input name="key" value="{escape(draft.key)}"></label>
<label>What do you expect to happen?<textarea name="hypothesis">{escape(draft.hypothesis)}</textarea></label>
<div class="grid"><div><label>Success metric<input name="primary_metric" value="{escape(draft.primary_metric)}"></label></div>
<div><label>Safety metric<input name="guardrail_metric" value="{escape(draft.guardrail_metric)}"></label>
<label>Maximum acceptable regression<input name="guardrail_threshold" type="number" step="0.001" value="{'' if draft.guardrail_maximum_regression is None else draft.guardrail_maximum_regression}"></label></div></div>
<details class="technical-details"><summary>Advanced experiment definition</summary>
  <div class="grid"><div><label>Eligibility rule<textarea name="eligibility_rule">{escape(draft.eligibility_rule)}</textarea></label>
  <label>Control allocation<input name="control_allocation" type="number" step="0.01" min="0.01" max="0.99" value="{draft.control_allocation}"></label>
  <label>Stopping rule<textarea name="stopping_rule">{escape(draft.stopping_rule)}</textarea></label></div>
  <div><label>Baseline rate<input name="baseline_rate" type="number" step="0.01" value="{draft.baseline_rate}"></label>
  <label>Target absolute effect<input name="target_effect" type="number" step="0.01" value="{draft.target_absolute_effect}"></label></div></div>
  <label>Typed variables (JSON)<textarea name="variables">{escape(variables_json)}</textarea></label>
  <label>Combination and sequence rules (JSON)<textarea name="constraints">{escape(constraints_json)}</textarea></label>
  <p class="muted">Use <code>effect: "exclude"</code> to remove a matching combination. Use <code>match_order: "in_order"</code> to validate the predicates as a sequence in declared factor order. Contradictory references or orderings block launch.</p>
</details>
<button type="submit">Validate</button>
</form>
{f'<div class="withheld" role="alert"><strong>Launch blocked</strong><ul>{error_html}</ul></div>' if errors else ''}
</div></details>

<details id="state-preview" class="workflow-step"><summary><span class="step-number">2</span><span class="step-title"><strong>States</strong></span><span class="step-state">{len(preview.valid)} valid · {len(preview.rejected)} rejected</span></summary><div class="step-body">
<table><thead><tr><th>Status</th><th>Typed values</th><th>Constraint result</th></tr></thead><tbody>{valid_rows}{rejected_rows}</tbody></table></div></details>

<details id="power" class="workflow-step"><summary><span class="step-number">3</span><span class="step-title"><strong>Power</strong></span><span class="step-state">{f'n={required_n:,} per state' if required_n is not None else 'needs input'}</span></summary><div class="step-body">
<div class="grid"><div><h3>Predeclared inputs</h3><p>Baseline {draft.baseline_rate:.1%} · target effect {draft.target_absolute_effect:.1%} · alpha {draft.alpha:.2f} · power {draft.power:.0%}</p></div>
<div><h3>Planning result</h3><p>{f'Required n={required_n:,} per state · implied MDE {mde:.2%}' if required_n is not None and mde is not None else 'Correct the power inputs.'}</p></div></div></div></details>

<details id="launch-review" class="workflow-step"><summary><span class="step-number">4</span><span class="step-title"><strong>Launch</strong></span><span class="step-state">draft v{draft.version}</span></summary><div class="step-body"><div class="review">
<p><strong>Hypothesis:</strong> {escape(draft.hypothesis)}</p><p><strong>Decision metric:</strong> {escape(draft.primary_metric)}</p>
<p><strong>Guardrail:</strong> {escape(draft.guardrail_metric)} cannot regress by more than {escape(str(draft.guardrail_maximum_regression))}</p>
<p><strong>Eligibility:</strong> {escape(draft.eligibility_rule)}</p><p><strong>Allocation:</strong> {draft.control_allocation:.0%} control / {1-draft.control_allocation:.0%} treatment</p>
<p><strong>Stopping rule:</strong> {escape(draft.stopping_rule)}</p></div>
<form method="post" action="/launch"><label><input style="width:auto" type="checkbox" name="immutable_review" value="yes"> I reviewed the inputs and understand launch freezes version {draft.version}.</label><button type="submit">Launch immutable v{draft.version}</button></form></div></details>

<details id="data-health" class="workflow-step"><summary><span class="step-number">5</span><span class="step-title"><strong>Data health</strong></span>{step_state(escape(health_step_state))}</summary><div class="step-body"><ul>{health_html}</ul>
<div class="actions"><form method="post" action="/health"><button class="secondary" name="failure" value="clear">Clear fixtures</button>
<button class="secondary" name="failure" value="srm">Inject SRM</button><button class="secondary" name="failure" value="missing_exposure">Inject missing exposure</button>
<button class="secondary" name="failure" value="ingestion_delay">Inject delay</button><button class="secondary" name="failure" value="guardrail_breach">Inject guardrail breach</button>
<button class="secondary" name="failure" value="version_disagreement">Inject version mismatch</button><button class="secondary" name="failure" value="stale_configuration">Inject stale config</button>
<button class="secondary" name="failure" value="underpowered">Inject underpowered readout</button></form></div></div></details>

<details id="readout" class="workflow-step"><summary><span class="step-number">6</span><span class="step-title"><strong>Results</strong></span>{step_state(readout_step_state)}</summary><div class="step-body">{live_html}<details class="technical-details"><summary>Decision fixture</summary>{effects_html}</details></div></details>

<details id="decision" class="workflow-step"><summary><span class="step-number">7</span><span class="step-title"><strong>Decision log</strong></span>{step_state(decision_step_state)}</summary><div class="step-body"><form method="post" action="/decision">
<label>Operator decision<select name="decision"><option>ship</option><option>iterate</option><option>stop</option><option>rollback</option></select></label>
<button type="submit"{disabled}>Record decision snapshot</button></form>
<table><thead><tr><th>Version</th><th>Decision</th><th>Created</th><th>Frozen evidence</th></tr></thead><tbody>{decision_rows}</tbody></table></div></details>

<details id="lifecycle" class="workflow-step"><summary><span class="step-number">8</span><span class="step-title"><strong>Lifecycle</strong></span>{step_state(lifecycle_step_state)}</summary><div class="step-body"><div class="actions">
<form method="post" action="/lifecycle"><button name="action" value="pause">Pause</button><button name="action" value="resume">Resume</button><button name="action" value="stop">Stop</button><button class="danger" name="action" value="kill">Kill switch</button><button name="action" value="rollback">Rollback</button><button class="secondary" name="action" value="clone">Clone to new version</button></form></div>
<table><thead><tr><th>Version</th><th>Lifecycle</th><th>Launched</th><th>Immutable configuration</th></tr></thead><tbody>{version_rows}</tbody></table></div></details>
</section>

<footer><p class="muted">Validation evidence comes from local simulations and tests; live telemetry reflects events ingested by this running sandbox.</p></footer>
</main>{live_script}</body></html>"""
    return html


def _parse_json_field(form: Mapping[str, list[str]], name: str) -> Any:
    raw = form.get(name, [""])[0]
    return json.loads(raw)


def draft_from_form(form: Mapping[str, list[str]], current: DashboardDraft) -> DashboardDraft:
    variables_raw = _parse_json_field(form, "variables")
    constraints_raw = _parse_json_field(form, "constraints")
    variables = tuple(
        VariableDefinition(
            str(item["name"]), VariableType(item["type"]), tuple(item["values"])
        )
        for item in variables_raw
    )
    constraints = tuple(
        ConstraintDefinition(
            item["when"],
            item.get("require", {}),
            str(item.get("reason", "Constraint rejected state.")),
            str(item.get("effect", "require")),
            str(item.get("match_order", "any_order")),
            tuple(item.get("sequence", tuple(item.get("when", {})))),
        )
        for item in constraints_raw
    )
    threshold_text = form.get("guardrail_threshold", [""])[0].strip()
    return DashboardDraft(
        key=form.get("key", [""])[0],
        version=current.version,
        hypothesis=form.get("hypothesis", [""])[0],
        primary_metric=form.get("primary_metric", [""])[0],
        guardrail_metric=form.get("guardrail_metric", [""])[0],
        guardrail_maximum_regression=float(threshold_text) if threshold_text else None,
        variables=variables,
        constraints=constraints,
        eligibility_rule=form.get("eligibility_rule", [""])[0],
        control_allocation=float(form.get("control_allocation", ["0"])[0]),
        stopping_rule=form.get("stopping_rule", [""])[0],
        baseline_rate=float(form.get("baseline_rate", ["0"])[0]),
        target_absolute_effect=float(form.get("target_effect", ["0"])[0]),
        alpha=current.alpha,
        power=current.power,
    )


def render_reference_product(
    sandbox: OperatorSandbox, result: Mapping[str, Any] | None = None, error: str | None = None
) -> str:
    current = sandbox.current_version
    experiment_name = sandbox.draft.key if current is None else deserialize_experiment(current.configuration_json).key
    result_html = ""
    if error:
        result_html = f"<div class='error' role='alert'><strong>Request blocked:</strong> {escape(error)}</div>"
    elif result:
        values = "".join(
            f"<li><strong>{escape(str(name))}:</strong> {escape(str(value))}</li>"
            for name, value in result["values"].items()
        )
        result_html = (
            "<div class='result'><span>SDK assignment</span>"
            f"<h2>{escape(str(result['state_key']))}</h2><ul>{values}</ul>"
            f"<p>Experiment v{int(result['version'])} · "
            f"{'outcome recorded' if result['converted'] else 'exposure recorded'}</p></div>"
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VariantGrid instrumented product</title><style>
:root{{--ink:#191d23;--muted:#667085;--accent:#3157d5;--line:#dfe3e8}}*{{box-sizing:border-box}}body{{margin:0;background:#f5f6f8;color:var(--ink);font:15px/1.5 Inter,system-ui,sans-serif}}main{{max-width:700px;margin:8vh auto;padding:36px;background:white;border:1px solid var(--line);border-radius:18px}}h1{{font-size:36px;line-height:1.05;margin:8px 0}}.eyebrow{{color:var(--accent);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.08em}}label{{display:block;font-weight:700;margin:24px 0 7px}}input{{width:100%;padding:12px;border:1px solid #cfd4d8;border-radius:9px;font:inherit}}button{{padding:11px 15px;border:0;border-radius:9px;background:var(--ink);color:white;font-weight:750;margin:12px 8px 0 0;cursor:pointer}}button.secondary{{background:#eef0f2;color:var(--ink)}}.result,.error{{margin-top:26px;padding:18px;border-radius:12px;background:#f0f4ff}}.error{{background:#fff1ef;color:#8a1c13}}.result h2{{margin:4px 0}}.result span,.result p,.muted{{color:var(--muted)}}a{{color:var(--accent);font-weight:700}}</style></head>
<body><main><span class="eyebrow">Deployed-product integration</span><h1>Instrumented onboarding</h1>
<p class="muted">This reference surface calls the Python SDK against the same API contract a deployed product uses. Assignment, exposure, and outcome events appear in the dashboard within two seconds.</p>
<form method="post" action="/reference-product"><label>User ID<input name="subject_id" value="reference-user-1" required maxlength="512"></label>
<button name="action" value="view">Load experience</button><button class="secondary" name="action" value="convert">Complete {escape(sandbox.draft.primary_metric)}</button></form>
{result_html}<p><a href="/">← View live dashboard</a></p><p class="muted">Active experiment: {escape(experiment_name)}</p></main></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    sandbox: OperatorSandbox

    def _send_json(self, status: int, body: Mapping[str, Any]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, status: int, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            payload = b'{"status":"ok"}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/v1/health":
            response = self.sandbox.api_request(
                APIRequest("GET", path, headers=dict(self.headers.items()))
            )
            self._send_json(response.status, response.body)
            return
        if path == "/api/live-readout":
            query = parse_qs(parsed.query)
            try:
                version_text = query.get("version", [""])[0]
                readout = self.sandbox.live_readout(
                    key=query.get("key", [None])[0],
                    version=int(version_text) if version_text else None,
                )
            except (KeyError, TypeError, ValueError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            else:
                self._send_json(HTTPStatus.OK, readout)
            return
        if path == "/reference-product":
            self._send_html(HTTPStatus.OK, render_reference_product(self.sandbox))
            return
        if path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = render_dashboard(self.sandbox).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        raw_body = self.rfile.read(length)
        path = urlparse(self.path).path
        if path in {"/v1/assign", "/v1/events/batch"}:
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                if not isinstance(body, Mapping):
                    raise ValueError("request body must be a JSON object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"schema_version": "1", "error": {"code": "invalid_json", "message": str(error)}},
                )
                return
            response = self.sandbox.api_request(
                APIRequest("POST", path, body, dict(self.headers.items()))
            )
            self._send_json(response.status, response.body)
            return
        form = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
        if path == "/reference-product":
            subject_id = form.get("subject_id", [""])[0].strip()
            try:
                if not subject_id:
                    raise ValueError("User ID is required.")
                if len(subject_id) > 512:
                    raise ValueError("User ID exceeds 512 characters.")
                result = self.sandbox.run_reference_product(
                    subject_id, convert=form.get("action", ["view"])[0] == "convert"
                )
            except (KeyError, RuntimeError, TypeError, ValueError) as error:
                self._send_html(
                    HTTPStatus.BAD_REQUEST,
                    render_reference_product(self.sandbox, error=str(error)),
                )
            else:
                self._send_html(
                    HTTPStatus.OK,
                    render_reference_product(self.sandbox, result=result),
                )
            return
        try:
            if path == "/assignment-lab":
                subject_id = form.get("subject_id", [""])[0].strip()
                if not subject_id:
                    raise ValueError("Assignment Lab requires a subject ID.")
                if len(subject_id) > 512:
                    raise ValueError("Assignment Lab subject ID exceeds 512 characters.")
                self.sandbox.assignment_subject = subject_id
                self.sandbox.messages.append(
                    f"Assignment evidence regenerated for subject {subject_id!r}."
                )
            elif path == "/preview":
                self.sandbox.update_draft(draft_from_form(form, self.sandbox.draft))
                self.sandbox.messages.append("Draft validated and state preview regenerated.")
            elif path == "/launch":
                self.sandbox.launch(review_confirmed=form.get("immutable_review") == ["yes"])
            elif path == "/health":
                self.sandbox.inject_failure(form.get("failure", [""])[0])
            elif path == "/decision":
                self.sandbox.record_decision(form.get("decision", [""])[0])
            elif path == "/lifecycle":
                action = form.get("action", [""])[0]
                if action == "clone":
                    self.sandbox.clone_current()
                elif action == "rollback":
                    self.sandbox.rollback()
                else:
                    self.sandbox.transition(action)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.sandbox.messages.append(f"Blocked: {error}")
        self._redirect()

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(host: str = "127.0.0.1", port: int = 8766) -> None:
    sandbox = OperatorSandbox()
    handler = type("BoundDashboardHandler", (DashboardHandler,), {"sandbox": sandbox})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"VariantGrid operator sandbox: http://{host}:{port}")
    print("Press Ctrl-C to stop. Demonstration only; do not connect production data.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local VariantGrid operator sandbox")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
