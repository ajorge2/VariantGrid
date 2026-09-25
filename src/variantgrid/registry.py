from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .models import (
    Experiment,
    Factor,
    Guardrail,
    MetricDefinition,
    Rule,
    VariableType,
    VariantState,
)
from .registry_migrations import LATEST_SCHEMA_VERSION, migrate_registry


CONFIGURATION_SCHEMA_VERSION = 2


class Lifecycle(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ARCHIVED = "archived"


_TRANSITIONS = {
    Lifecycle.DRAFT: {Lifecycle.RUNNING, Lifecycle.ARCHIVED},
    Lifecycle.RUNNING: {Lifecycle.PAUSED, Lifecycle.STOPPED},
    Lifecycle.PAUSED: {Lifecycle.RUNNING, Lifecycle.STOPPED},
    Lifecycle.STOPPED: {Lifecycle.ARCHIVED},
    Lifecycle.ARCHIVED: set(),
}


@dataclass(frozen=True)
class RegisteredVersion:
    experiment: Experiment
    lifecycle: Lifecycle
    configuration_json: str
    configuration_sha256: str = ""
    schema_version: int = CONFIGURATION_SCHEMA_VERSION
    created_at: str | None = None
    launched_at: str | None = None


@dataclass(frozen=True)
class DecisionRecord:
    experiment_key: str
    version: int
    decision: str
    evidence: Mapping[str, Any]
    created_at: str


def _experiment_payload(experiment: Experiment) -> dict[str, Any]:
    return {
        "configuration_schema_version": CONFIGURATION_SCHEMA_VERSION,
        "key": experiment.key,
        "version": experiment.version,
        "hypothesis": experiment.hypothesis,
        "factors": [
            {
                "name": factor.name,
                "values": list(factor.values),
                "variable_type": factor.variable_type.value,
                "active_when": dict(factor.active_when),
            }
            for factor in experiment.factors
        ],
        "rules": [
            {"when": dict(rule.when), "require": dict(rule.require), "reason": rule.reason}
            for rule in experiment.rules
        ],
        "states": [
            {"key": state.key, "values": dict(state.values), "weight": state.weight}
            for state in experiment.states
        ],
        "metrics": [
            {"name": metric.name, "metric_type": metric.metric_type, "role": metric.role}
            for metric in experiment.metrics
        ],
        "primary_metric": experiment.primary_metric,
        "salt": experiment.salt,
        "guardrails": [
            {"metric": guardrail.metric, "maximum_regression": guardrail.maximum_regression}
            for guardrail in experiment.guardrails
        ],
        "minimum_sample_size": experiment.minimum_sample_size,
        "assignment_algorithm_version": experiment.assignment_algorithm_version,
        "policy_version": experiment.policy_version,
        "eligibility_rule": experiment.eligibility_rule,
        "attribution_window_hours": experiment.attribution_window_hours,
        "stopping_rule": experiment.stopping_rule,
        "statistical_method": experiment.statistical_method,
    }


def serialize_experiment(experiment: Experiment) -> str:
    return json.dumps(
        _experiment_payload(experiment), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def configuration_sha256(configuration_json: str) -> str:
    return sha256(configuration_json.encode("utf-8")).hexdigest()


def deserialize_experiment(payload: str) -> Experiment:
    raw = json.loads(payload)
    factors = tuple(
        Factor(
            item["name"],
            tuple(item["values"]),
            VariableType(item.get("variable_type", VariableType.ENUM.value)),
            item.get("active_when", {}),
        )
        for item in raw.get("factors", ())
    )
    rules = tuple(
        Rule(item["when"], item["require"], item.get("reason", "constraint_not_satisfied"))
        for item in raw.get("rules", ())
    )
    metrics = tuple(MetricDefinition(**item) for item in raw.get("metrics", ()))
    return Experiment(
        key=raw["key"],
        version=raw["version"],
        states=tuple(
            VariantState(item["key"], item["values"], item["weight"])
            for item in raw["states"]
        ),
        primary_metric=raw["primary_metric"],
        salt=raw["salt"],
        guardrails=tuple(Guardrail(**item) for item in raw.get("guardrails", ())),
        minimum_sample_size=raw.get("minimum_sample_size", 0),
        assignment_algorithm_version=raw.get("assignment_algorithm_version", "sha256-v1"),
        policy_version=raw.get("policy_version", "fixed-weight-v1"),
        eligibility_rule=raw.get("eligibility_rule", "all"),
        attribution_window_hours=raw.get("attribution_window_hours", 168),
        stopping_rule=raw.get("stopping_rule", "fixed-horizon"),
        statistical_method=raw.get("statistical_method", "two-proportion-z-v1"),
        hypothesis=raw.get("hypothesis", ""),
        factors=factors,
        rules=rules,
        metrics=metrics,
    )


def _validate_replay(experiment: Experiment) -> None:
    if not experiment.states:
        raise ValueError("An experiment version cannot have an empty state space")
    if not experiment.factors:
        # Backward-compatible hand-authored experiments have no source factors;
        # their frozen state list remains their replay input.
        return
    replayed = experiment.replay_states()
    stored = tuple(
        (state.key, state.state_id, state.canonical_json, state.weight)
        for state in experiment.states
    )
    regenerated = tuple(
        (state.key, state.state_id, state.canonical_json, state.weight) for state in replayed
    )
    if regenerated != stored:
        raise ValueError("Stored states do not equal deterministic state-generation replay")


class ExperimentRegistry:
    """Persistent source of truth with immutable launched configurations."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self._closed = False
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        migrate_registry(self.connection)
        self._backfill_v2_derivatives()

    @property
    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        if not self._closed:
            self.connection.close()
            self._closed = True

    def __enter__(self) -> "ExperimentRegistry":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        # A fallback for short-lived local/demo registries. Long-lived services
        # should still use an explicit context manager so closure is observable.
        try:
            self.close()
        except Exception:
            pass

    def _backfill_v2_derivatives(self) -> None:
        """Populate hashes and normalized records when opening a v1 database."""
        rows = self.connection.execute("SELECT * FROM experiment_versions").fetchall()
        with self.connection:
            for row in rows:
                payload = str(row["configuration_json"])
                digest = configuration_sha256(payload)
                if not row["configuration_sha256"]:
                    self.connection.execute(
                        "UPDATE experiment_versions SET configuration_sha256 = ? "
                        "WHERE experiment_key = ? AND version = ?",
                        (digest, row["experiment_key"], row["version"]),
                    )
                experiment = deserialize_experiment(payload)
                self._upsert_experiment_definition(experiment)
                self._replace_derivatives(experiment)

    def _upsert_experiment_definition(self, experiment: Experiment) -> None:
        existing = self.connection.execute(
            "SELECT hypothesis FROM experiments WHERE experiment_key = ?", (experiment.key,)
        ).fetchone()
        if existing is None:
            self.connection.execute(
                "INSERT INTO experiments (experiment_key, hypothesis) VALUES (?, ?)",
                (experiment.key, experiment.hypothesis),
            )
        elif experiment.hypothesis and not existing["hypothesis"]:
            self.connection.execute(
                "UPDATE experiments SET hypothesis = ? WHERE experiment_key = ?",
                (experiment.hypothesis, experiment.key),
            )

    def _replace_derivatives(self, experiment: Experiment) -> None:
        self.connection.execute(
            "DELETE FROM metrics WHERE experiment_key = ? AND version = ?",
            (experiment.key, experiment.version),
        )
        self.connection.execute(
            "DELETE FROM guardrails WHERE experiment_key = ? AND version = ?",
            (experiment.key, experiment.version),
        )
        metrics = experiment.metrics or (MetricDefinition(experiment.primary_metric),)
        self.connection.executemany(
            "INSERT INTO metrics VALUES (?, ?, ?, ?, ?)",
            [
                (experiment.key, experiment.version, metric.name, metric.metric_type, metric.role)
                for metric in metrics
            ],
        )
        self.connection.executemany(
            "INSERT INTO guardrails VALUES (?, ?, ?, ?)",
            [
                (
                    experiment.key,
                    experiment.version,
                    guardrail.metric,
                    guardrail.maximum_regression,
                )
                for guardrail in experiment.guardrails
            ],
        )

    def create(self, experiment: Experiment) -> RegisteredVersion:
        _validate_replay(experiment)
        payload = serialize_experiment(experiment)
        digest = configuration_sha256(payload)
        try:
            with self.connection:
                self._upsert_experiment_definition(experiment)
                self.connection.execute(
                    """
                    INSERT INTO experiment_versions
                        (experiment_key, version, lifecycle, configuration_json,
                         configuration_sha256, schema_version, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        experiment.key,
                        experiment.version,
                        Lifecycle.DRAFT.value,
                        payload,
                        digest,
                        CONFIGURATION_SCHEMA_VERSION,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self._replace_derivatives(experiment)
        except sqlite3.IntegrityError as error:
            raise ValueError("Experiment version already exists") from error
        return self.get(experiment.key, experiment.version)

    def get(self, key: str, version: int) -> RegisteredVersion:
        row = self.connection.execute(
            "SELECT * FROM experiment_versions WHERE experiment_key = ? AND version = ?",
            (key, version),
        ).fetchone()
        if row is None:
            raise KeyError((key, version))
        payload = str(row["configuration_json"])
        stored_digest = row["configuration_sha256"] or configuration_sha256(payload)
        if stored_digest != configuration_sha256(payload):
            raise ValueError("Registry configuration checksum mismatch")
        experiment = deserialize_experiment(payload)
        _validate_replay(experiment)
        return RegisteredVersion(
            experiment=experiment,
            lifecycle=Lifecycle(row["lifecycle"]),
            configuration_json=payload,
            configuration_sha256=stored_digest,
            schema_version=int(row["schema_version"] or 1),
            created_at=row["created_at"],
            launched_at=row["launched_at"],
        )

    def replace_draft(self, experiment: Experiment) -> RegisteredVersion:
        current = self.get(experiment.key, experiment.version)
        if current.lifecycle is not Lifecycle.DRAFT:
            raise ValueError("Launched experiment versions are immutable")
        _validate_replay(experiment)
        payload = serialize_experiment(experiment)
        with self.connection:
            self.connection.execute(
                """
                UPDATE experiment_versions
                SET configuration_json = ?, configuration_sha256 = ?, schema_version = ?
                WHERE experiment_key = ? AND version = ?
                """,
                (
                    payload,
                    configuration_sha256(payload),
                    CONFIGURATION_SCHEMA_VERSION,
                    experiment.key,
                    experiment.version,
                ),
            )
            self._upsert_experiment_definition(experiment)
            self._replace_derivatives(experiment)
        return self.get(experiment.key, experiment.version)

    def transition(self, key: str, version: int, target: Lifecycle) -> RegisteredVersion:
        current = self.get(key, version)
        if target not in _TRANSITIONS[current.lifecycle]:
            raise ValueError(f"Invalid lifecycle transition: {current.lifecycle.value} -> {target.value}")
        _validate_replay(current.experiment)
        launched_at = current.launched_at
        if current.lifecycle is Lifecycle.DRAFT and target is Lifecycle.RUNNING:
            launched_at = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                UPDATE experiment_versions SET lifecycle = ?, launched_at = ?
                WHERE experiment_key = ? AND version = ?
                """,
                (target.value, launched_at, key, version),
            )
        return self.get(key, version)

    def clone_version(self, key: str, source_version: int, new_version: int) -> RegisteredVersion:
        if new_version <= source_version:
            raise ValueError("A cloned version must increase the version number")
        source = self.get(key, source_version).experiment
        return self.create(replace(source, version=new_version))

    def clone_with_edits(
        self,
        key: str,
        source_version: int,
        *,
        new_version: int | None = None,
        **changes: Any,
    ) -> RegisteredVersion:
        """Apply material changes only by cloning to a new draft version."""
        source = self.get(key, source_version).experiment
        forbidden = {"key", "version"} & set(changes)
        if forbidden:
            raise ValueError(f"clone_with_edits cannot change {sorted(forbidden)}")
        if new_version is None:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM experiment_versions WHERE experiment_key = ?",
                (key,),
            ).fetchone()
            new_version = int(row[0]) + 1
        if new_version <= source_version:
            raise ValueError("Material edits require a newer experiment version")
        candidate = replace(source, version=new_version, **changes)
        return self.create(candidate)

    def record_decision(self, key: str, version: int, decision: str, evidence: dict) -> DecisionRecord:
        current = self.get(key, version)
        if current.lifecycle not in (Lifecycle.RUNNING, Lifecycle.PAUSED, Lifecycle.STOPPED):
            raise ValueError("Decisions require a launched experiment version")
        if not decision.strip() or not evidence:
            raise ValueError("Decisions require a decision and non-empty evidence snapshot")
        evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO decisions
                    (experiment_key, version, decision, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, version, decision, evidence_json, created_at),
            )
        return DecisionRecord(key, version, decision, json.loads(evidence_json), created_at)

    def decisions(self, key: str, version: int) -> tuple[DecisionRecord, ...]:
        self.get(key, version)
        rows = self.connection.execute(
            """
            SELECT experiment_key, version, decision, evidence_json, created_at
            FROM decisions WHERE experiment_key = ? AND version = ?
            ORDER BY created_at, rowid
            """,
            (key, version),
        ).fetchall()
        return tuple(
            DecisionRecord(
                row["experiment_key"],
                row["version"],
                row["decision"],
                json.loads(row["evidence_json"]),
                row["created_at"],
            )
            for row in rows
        )

    def metrics(self, key: str, version: int) -> tuple[MetricDefinition, ...]:
        self.get(key, version)
        rows = self.connection.execute(
            """
            SELECT metric_name, metric_type, metric_role FROM metrics
            WHERE experiment_key = ? AND version = ? ORDER BY metric_name
            """,
            (key, version),
        ).fetchall()
        return tuple(
            MetricDefinition(row["metric_name"], row["metric_type"], row["metric_role"])
            for row in rows
        )

    def guardrails(self, key: str, version: int) -> tuple[Guardrail, ...]:
        self.get(key, version)
        rows = self.connection.execute(
            """
            SELECT metric_name, maximum_regression FROM guardrails
            WHERE experiment_key = ? AND version = ? ORDER BY metric_name
            """,
            (key, version),
        ).fetchall()
        return tuple(Guardrail(row["metric_name"], row["maximum_regression"]) for row in rows)

    def replay(self, key: str, version: int) -> Experiment:
        """Regenerate states and prove byte-stable assignment inputs."""
        stored = self.get(key, version).experiment
        replayed = replace(stored, states=stored.replay_states()) if stored.factors else stored
        if serialize_experiment(replayed) != serialize_experiment(stored):
            raise ValueError("Registry replay changed frozen assignment inputs")
        return replayed

    def persistence_snapshot(self, key: str, version: int) -> dict[str, Any]:
        registered = self.get(key, version)
        return {
            "experiment_key": key,
            "version": version,
            "lifecycle": registered.lifecycle.value,
            "configuration_sha256": registered.configuration_sha256,
            "schema_version": registered.schema_version,
            "metrics": [metric.__dict__ for metric in self.metrics(key, version)],
            "guardrails": [guardrail.__dict__ for guardrail in self.guardrails(key, version)],
            "decisions": [
                {
                    "decision": record.decision,
                    "evidence": dict(record.evidence),
                    "created_at": record.created_at,
                }
                for record in self.decisions(key, version)
            ],
        }


__all__ = [
    "CONFIGURATION_SCHEMA_VERSION",
    "LATEST_SCHEMA_VERSION",
    "DecisionRecord",
    "ExperimentRegistry",
    "Lifecycle",
    "RegisteredVersion",
    "configuration_sha256",
    "deserialize_experiment",
    "serialize_experiment",
]
