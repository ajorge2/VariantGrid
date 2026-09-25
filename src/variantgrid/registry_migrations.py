from __future__ import annotations

import sqlite3


LATEST_SCHEMA_VERSION = 2


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migrate_registry(connection: sqlite3.Connection, target: int = LATEST_SCHEMA_VERSION) -> int:
    """Apply transactional SQLite registry migrations, including baseline upgrades."""
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if target < current or target > LATEST_SCHEMA_VERSION:
        raise ValueError(f"Cannot migrate registry from {current} to {target}")

    with connection:
        if current < 1 and target >= 1:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiment_versions (
                    experiment_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    lifecycle TEXT NOT NULL,
                    configuration_json TEXT NOT NULL,
                    PRIMARY KEY (experiment_key, version)
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    experiment_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                PRAGMA user_version = 1;
                """
            )
            current = 1

        if current < 2 and target >= 2:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_key TEXT PRIMARY KEY,
                    hypothesis TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS metrics (
                    experiment_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    metric_role TEXT NOT NULL,
                    PRIMARY KEY (experiment_key, version, metric_name),
                    FOREIGN KEY (experiment_key, version)
                        REFERENCES experiment_versions(experiment_key, version)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS guardrails (
                    experiment_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    metric_name TEXT NOT NULL,
                    maximum_regression REAL NOT NULL,
                    PRIMARY KEY (experiment_key, version, metric_name),
                    FOREIGN KEY (experiment_key, version)
                        REFERENCES experiment_versions(experiment_key, version)
                        ON DELETE CASCADE
                );
                """
            )
            _add_column(connection, "experiment_versions", "configuration_sha256 TEXT")
            _add_column(connection, "experiment_versions", "schema_version INTEGER NOT NULL DEFAULT 2")
            _add_column(connection, "experiment_versions", "created_at TEXT")
            _add_column(connection, "experiment_versions", "launched_at TEXT")
            connection.execute(
                "UPDATE experiment_versions SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)"
            )
            connection.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS immutable_launched_configuration
                BEFORE UPDATE OF configuration_json, configuration_sha256, schema_version
                ON experiment_versions
                WHEN OLD.lifecycle <> 'draft' AND OLD.configuration_sha256 IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'launched experiment versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_launched_version_delete
                BEFORE DELETE ON experiment_versions
                WHEN OLD.lifecycle <> 'draft'
                BEGIN
                    SELECT RAISE(ABORT, 'launched experiment versions cannot be deleted');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_decision_update
                BEFORE UPDATE ON decisions
                BEGIN
                    SELECT RAISE(ABORT, 'decision records are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS immutable_decision_delete
                BEFORE DELETE ON decisions
                BEGIN
                    SELECT RAISE(ABORT, 'decision records are immutable');
                END;
                PRAGMA user_version = 2;
                """
            )
            current = 2
    return current


def rollback_registry(connection: sqlite3.Connection, target: int = 1) -> int:
    """Roll schema 2 back to the v1-compatible surface without data loss."""
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if target != 1 or current != 2:
        raise ValueError(f"Supported rollback is schema 2 to 1, not {current} to {target}")
    with connection:
        connection.executescript(
            """
            DROP TRIGGER IF EXISTS immutable_launched_configuration;
            DROP TRIGGER IF EXISTS immutable_launched_version_delete;
            DROP TRIGGER IF EXISTS immutable_decision_update;
            DROP TRIGGER IF EXISTS immutable_decision_delete;
            DROP TABLE IF EXISTS guardrails;
            DROP TABLE IF EXISTS metrics;
            DROP TABLE IF EXISTS experiments;
            PRAGMA user_version = 1;
            """
        )
    return 1
