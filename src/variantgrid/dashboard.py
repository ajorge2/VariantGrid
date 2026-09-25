from __future__ import annotations

"""Dependency-light VariantGrid operator dashboard and sandbox.

The dashboard is intentionally a local demonstration surface. It makes the
product workflow and integrity gates executable without claiming that an
in-memory process is a production registry, event store, or authorization
boundary.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
import argparse
import json
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .analysis import evaluate_binary_decision, minimum_detectable_effect, required_sample_size
from .models import Experiment, Factor, Guardrail, Rule, VariableType
from .registry import serialize_experiment


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "when", MappingProxyType(dict(self.when)))
        object.__setattr__(self, "require", MappingProxyType(dict(self.require)))

    def rule(self) -> Rule:
        return Rule(self.when, self.require)

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
    states: list[PreviewState] = []
    for combination in product(*(variable.values for variable in draft.variables)):
        values = dict(zip(names, combination, strict=True))
        reasons = tuple(rule.reason for rule in draft.constraints if not rule.accepts(values))
        states.append(PreviewState(values, not reasons, reasons))
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
    try:
        for variable in draft.variables:
            variable.factor()
    except ValueError as error:
        errors.append(f"Variable definition is invalid: {error}")
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


class OperatorSandbox:
    """Single-process workflow store used by the local operator demo."""

    def __init__(self, draft: DashboardDraft | None = None) -> None:
        self.draft = draft or default_draft()
        self.versions: dict[int, LaunchedVersion] = {}
        self.deployed_version: int | None = None
        self.health = DataHealth()
        self.metrics = MetricFixture()
        self.decisions: list[DecisionSnapshot] = []
        self.messages: list[str] = []

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


def render_dashboard(sandbox: OperatorSandbox) -> str:
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
            {"when": dict(item.when), "require": dict(item.require), "reason": item.reason}
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

    lifecycle = launched.lifecycle.value if launched else "not launched"
    decision_lifecycle_ok = launched is not None and launched.lifecycle in (
        DashboardLifecycle.RUNNING,
        DashboardLifecycle.PAUSED,
    )
    disabled = " disabled" if health_failures or not decision_lifecycle_ok else ""
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VariantGrid operator sandbox</title>
<style>
:root{{--ink:#e7edf7;--muted:#9dacbf;--panel:#151d29;--line:#2b3a4e;--blue:#6da8ff;--good:#75d6a0;--bad:#ff858c;--bg:#0b1119}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:1180px;margin:auto;padding:32px}} h1{{font-size:34px;margin-bottom:4px}} h2{{margin-top:0}} h3{{margin-bottom:8px}}
.lede,.muted{{color:var(--muted)}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:18px}}
section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;margin:18px 0}}
label{{display:block;font-weight:650;margin:10px 0 4px}} input,textarea{{width:100%;background:#0e1621;color:var(--ink);border:1px solid #40536b;border-radius:8px;padding:9px}}
textarea{{min-height:84px}} button{{background:var(--blue);border:0;border-radius:8px;padding:9px 13px;font-weight:750;margin:4px;cursor:pointer}}
button.secondary{{background:#30425a;color:var(--ink)}} button.danger{{background:#9d3e49;color:white}} button:disabled{{opacity:.38;cursor:not-allowed}}
table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;border-bottom:1px solid var(--line);padding:9px;vertical-align:top}} code{{white-space:pre-wrap;color:#bad4ff}}
.badge{{display:inline-block;border-radius:99px;padding:5px 9px;margin:3px}} .good{{background:#173d2c;color:var(--good)}} .bad{{background:#4c2228;color:var(--bad)}}
.withheld{{border:2px solid var(--bad);background:#311d24;padding:18px;border-radius:10px}} .review{{border-left:4px solid var(--blue);padding-left:15px}}
.rejected{{color:#f3adb1}} .bar-row{{display:flex;align-items:center;gap:10px;margin:11px 0}} .bar-row span{{width:210px}} .bar-row i{{height:20px;background:var(--blue);display:inline-block;border-radius:4px}}
.statusline{{display:flex;gap:12px;flex-wrap:wrap}} .statusline span{{background:#202c3c;padding:5px 10px;border-radius:99px}}
.actions form{{display:inline-block}} ul.messages{{color:#bcd5ff}} @media(max-width:600px){{main{{padding:18px}}}}
</style>
</head>
<body><main>
<header><h1>VariantGrid operator sandbox</h1><p class="lede">Configure a constrained experiment, review immutable inputs, then see why data health must pass before effects or decisions appear.</p>
<div class="statusline"><span>Draft v{draft.version}</span><span>Deployed: {escape(str(sandbox.deployed_version or 'none'))}</span><span>Lifecycle: {escape(lifecycle)}</span></div></header>
<ul class="messages" aria-live="polite">{messages}</ul>

<section id="configuration"><h2>1. Decision contract</h2>
<form method="post" action="/preview">
<div class="grid"><div>
<label>Experiment key<input name="key" value="{escape(draft.key)}"></label>
<label>Hypothesis<textarea name="hypothesis">{escape(draft.hypothesis)}</textarea></label>
<label>Primary metric<input name="primary_metric" value="{escape(draft.primary_metric)}"></label>
<label>Guardrail metric<input name="guardrail_metric" value="{escape(draft.guardrail_metric)}"></label>
<label>Maximum guardrail regression<input name="guardrail_threshold" type="number" step="0.001" value="{'' if draft.guardrail_maximum_regression is None else draft.guardrail_maximum_regression}"></label>
</div><div>
<label>Eligibility rule<textarea name="eligibility_rule">{escape(draft.eligibility_rule)}</textarea></label>
<label>Control allocation<input name="control_allocation" type="number" step="0.01" min="0.01" max="0.99" value="{draft.control_allocation}"></label>
<label>Stopping rule<textarea name="stopping_rule">{escape(draft.stopping_rule)}</textarea></label>
<label>Baseline rate<input name="baseline_rate" type="number" step="0.01" value="{draft.baseline_rate}"></label>
<label>Target absolute effect<input name="target_effect" type="number" step="0.01" value="{draft.target_absolute_effect}"></label>
</div></div>
<label>Typed variables (JSON)<textarea name="variables">{escape(variables_json)}</textarea></label>
<label>Constraints and rejection reasons (JSON)<textarea name="constraints">{escape(constraints_json)}</textarea></label>
<button type="submit">Validate and preview states</button>
</form>
{f'<div class="withheld" role="alert"><strong>Launch blocked</strong><ul>{error_html}</ul></div>' if errors else '<p><span class="badge good">Configuration complete</span></p>'}
</section>

<section id="state-preview"><h2>2. Constraint-aware state preview</h2><p>{len(preview.valid)} valid · {len(preview.rejected)} rejected. Rejected combinations remain visible with reasons.</p>
<table><thead><tr><th>Status</th><th>Typed values</th><th>Constraint result</th></tr></thead><tbody>{valid_rows}{rejected_rows}</tbody></table></section>

<section id="power"><h2>3. Power and MDE review</h2>
<div class="grid"><div><h3>Predeclared inputs</h3><p>Baseline {draft.baseline_rate:.1%} · target effect {draft.target_absolute_effect:.1%} · alpha {draft.alpha:.2f} · power {draft.power:.0%}</p></div>
<div><h3>Planning result</h3><p>{f'Required n={required_n:,} per state · implied MDE {mde:.2%}' if required_n is not None and mde is not None else 'Correct the power inputs.'}</p></div></div></section>

<section id="launch-review"><h2>4. Immutable launch review</h2><div class="review">
<p><strong>Hypothesis:</strong> {escape(draft.hypothesis)}</p><p><strong>Decision metric:</strong> {escape(draft.primary_metric)}</p>
<p><strong>Guardrail:</strong> {escape(draft.guardrail_metric)} cannot regress by more than {escape(str(draft.guardrail_maximum_regression))}</p>
<p><strong>Eligibility:</strong> {escape(draft.eligibility_rule)}</p><p><strong>Allocation:</strong> {draft.control_allocation:.0%} control / {1-draft.control_allocation:.0%} treatment</p>
<p><strong>Stopping rule:</strong> {escape(draft.stopping_rule)}</p></div>
<form method="post" action="/launch"><label><input style="width:auto" type="checkbox" name="immutable_review" value="yes"> I reviewed the inputs and understand launch freezes version {draft.version}.</label><button type="submit">Launch immutable v{draft.version}</button></form></section>

<section id="data-health"><h2>5. Data health comes first</h2><p class="muted">These gates are evaluated before effect estimates or a recommendation.</p><ul>{health_html}</ul>
<div class="actions"><form method="post" action="/health"><button class="secondary" name="failure" value="clear">Clear fixtures</button>
<button class="secondary" name="failure" value="srm">Inject SRM</button><button class="secondary" name="failure" value="missing_exposure">Inject missing exposure</button>
<button class="secondary" name="failure" value="ingestion_delay">Inject delay</button><button class="secondary" name="failure" value="guardrail_breach">Inject guardrail breach</button>
<button class="secondary" name="failure" value="version_disagreement">Inject version mismatch</button><button class="secondary" name="failure" value="stale_configuration">Inject stale config</button>
<button class="secondary" name="failure" value="underpowered">Inject underpowered readout</button></form></div></section>

<section id="readout"><h2>6. Effect and uncertainty</h2>{effects_html}</section>

<section id="decision"><h2>7. Decision and immutable evidence</h2><form method="post" action="/decision">
<label>Operator decision<select name="decision"><option>ship</option><option>iterate</option><option>stop</option><option>rollback</option></select></label>
<button type="submit"{disabled}>Record decision snapshot</button></form>
<table><thead><tr><th>Version</th><th>Decision</th><th>Created</th><th>Frozen evidence</th></tr></thead><tbody>{decision_rows}</tbody></table></section>

<section id="lifecycle"><h2>8. Lifecycle and version history</h2><div class="actions">
<form method="post" action="/lifecycle"><button name="action" value="pause">Pause</button><button name="action" value="resume">Resume</button><button name="action" value="stop">Stop</button><button class="danger" name="action" value="kill">Kill switch</button><button name="action" value="rollback">Rollback</button><button class="secondary" name="action" value="clone">Clone to new version</button></form></div>
<table><thead><tr><th>Version</th><th>Lifecycle</th><th>Launched</th><th>Immutable configuration</th></tr></thead><tbody>{version_rows}</tbody></table></section>

<footer><p class="muted">Local sandbox only. UI rendering does not prove distributed durability, authentication, production ingestion, or causal business impact.</p></footer>
</main></body></html>"""
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
        ConstraintDefinition(item["when"], item["require"], str(item.get("reason", "Constraint rejected state.")))
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


class DashboardHandler(BaseHTTPRequestHandler):
    sandbox: OperatorSandbox

    def _redirect(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if urlparse(self.path).path != "/":
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
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        path = urlparse(self.path).path
        try:
            if path == "/preview":
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
    print("Press Ctrl-C to stop. Local demonstration only; do not expose to untrusted networks.")
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
