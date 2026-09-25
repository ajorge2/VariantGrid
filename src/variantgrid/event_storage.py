from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterator, Mapping, Protocol, Sequence


class EventIdCollisionError(ValueError):
    """An existing event ID was reused for a different immutable payload."""


class BatchIdCollisionError(ValueError):
    """An existing batch ID was reused for a different ordered payload."""


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    experiment_key: str
    experiment_version: int
    subject_id: str
    event_type: str
    variant_key: str | None
    metric: str | None
    value: float | None
    occurred_at: str
    received_at: str
    schema_version: int
    payload_hash: str


@dataclass(frozen=True)
class DeadLetterRecord:
    payload_json: str
    error_code: str
    error_message: str
    received_at: str


@dataclass(frozen=True)
class BatchWriteResult:
    inserted: int
    duplicates: int
    dead_lettered: int
    replayed: bool = False


class EventRepository(Protocol):
    """Persistence contract implementable by SQLite, Postgres, or a test double.

    Implementations must make event insertion and batch-result recording atomic,
    and must serialize competing writes for the same event or batch ID.
    """

    def insert_event(self, record: EventRecord) -> bool: ...

    def insert_batch(
        self,
        batch_id: str,
        batch_hash: str,
        records: Sequence[EventRecord],
        dead_letters: Sequence[DeadLetterRecord],
        received_at: str,
    ) -> BatchWriteResult: ...

    def add_dead_letter(self, record: DeadLetterRecord) -> int: ...

    def fetch_events(
        self,
        experiment_key: str | None = None,
        experiment_version: int | None = None,
        *,
        order: str = "event",
    ) -> list[Mapping[str, object]]: ...

    def list_dead_letters(self, status: str = "pending") -> list[Mapping[str, object]]: ...

    def get_dead_letter(self, dead_letter_id: int) -> Mapping[str, object]: ...

    def mark_dead_letter_resolved(self, dead_letter_id: int, resolved_at: str) -> None: ...

    def mark_dead_letter_attempt(self, dead_letter_id: int, error_message: str) -> None: ...

    def count_events(self, event_type: str | None = None) -> int: ...

    def close(self) -> None: ...


class SQLiteEventRepository:
    """Deterministic local adapter for the event persistence contract.

    SQLite proves local transactional semantics. It deliberately does not claim
    Postgres concurrency, durability, availability, or throughput behavior.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self._closed = False
        self.connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                experiment_key TEXT NOT NULL,
                experiment_version INTEGER NOT NULL,
                subject_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('assignment','exposure','goal','observation','guardrail')),
                variant_key TEXT,
                metric TEXT,
                value REAL,
                occurred_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                payload_hash TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS event_lookup
                ON events(experiment_key, experiment_version, event_type, metric, subject_id);
            CREATE INDEX IF NOT EXISTS event_time_lookup
                ON events(experiment_key, experiment_version, occurred_at, event_id);

            CREATE TABLE IF NOT EXISTS ingestion_batches (
                batch_id TEXT PRIMARY KEY,
                batch_hash TEXT NOT NULL,
                inserted_count INTEGER NOT NULL,
                duplicate_count INTEGER NOT NULL,
                dead_letter_count INTEGER NOT NULL,
                received_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dead_letters (
                dead_letter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload_json TEXT NOT NULL,
                error_code TEXT NOT NULL,
                error_message TEXT NOT NULL,
                received_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                resolved_at TEXT
            );
            """
        )
        self.connection.commit()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    @staticmethod
    def _event_values(record: EventRecord) -> tuple[object, ...]:
        return (
            record.event_id,
            record.experiment_key,
            record.experiment_version,
            record.subject_id,
            record.event_type,
            record.variant_key,
            record.metric,
            record.value,
            record.occurred_at,
            record.received_at,
            record.schema_version,
            record.payload_hash,
        )

    def _insert_event(self, connection: sqlite3.Connection, record: EventRecord) -> bool:
        existing = connection.execute(
            "SELECT payload_hash FROM events WHERE event_id = ?", (record.event_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] == record.payload_hash:
                return False
            raise EventIdCollisionError(
                f"event_id {record.event_id!r} already belongs to a different payload"
            )
        connection.execute(
            """
            INSERT INTO events (
                event_id, experiment_key, experiment_version, subject_id,
                event_type, variant_key, metric, value, occurred_at, received_at,
                schema_version, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._event_values(record),
        )
        return True

    def insert_event(self, record: EventRecord) -> bool:
        with self._transaction() as connection:
            return self._insert_event(connection, record)

    def insert_batch(
        self,
        batch_id: str,
        batch_hash: str,
        records: Sequence[EventRecord],
        dead_letters: Sequence[DeadLetterRecord],
        received_at: str,
    ) -> BatchWriteResult:
        with self._transaction() as connection:
            existing_batch = connection.execute(
                "SELECT * FROM ingestion_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if existing_batch is not None:
                if existing_batch["batch_hash"] != batch_hash:
                    raise BatchIdCollisionError(
                        f"batch_id {batch_id!r} already belongs to a different payload"
                    )
                return BatchWriteResult(
                    inserted=existing_batch["inserted_count"],
                    duplicates=existing_batch["duplicate_count"],
                    dead_lettered=existing_batch["dead_letter_count"],
                    replayed=True,
                )

            inserted = 0
            duplicates = 0
            generated_dead_letters = list(dead_letters)
            for record in records:
                try:
                    if self._insert_event(connection, record):
                        inserted += 1
                    else:
                        duplicates += 1
                except EventIdCollisionError as error:
                    generated_dead_letters.append(
                        DeadLetterRecord(
                            payload_json=_record_json(record),
                            error_code="event_id_collision",
                            error_message=str(error),
                            received_at=received_at,
                        )
                    )

            for letter in generated_dead_letters:
                self._add_dead_letter(connection, letter)
            result = BatchWriteResult(inserted, duplicates, len(generated_dead_letters))
            connection.execute(
                """
                INSERT INTO ingestion_batches (
                    batch_id, batch_hash, inserted_count, duplicate_count,
                    dead_letter_count, received_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    batch_hash,
                    result.inserted,
                    result.duplicates,
                    result.dead_lettered,
                    received_at,
                ),
            )
            return result

    @staticmethod
    def _add_dead_letter(connection: sqlite3.Connection, record: DeadLetterRecord) -> int:
        cursor = connection.execute(
            """
            INSERT INTO dead_letters (
                payload_json, error_code, error_message, received_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                record.payload_json,
                record.error_code,
                record.error_message,
                record.received_at,
            ),
        )
        return int(cursor.lastrowid)

    def add_dead_letter(self, record: DeadLetterRecord) -> int:
        with self._transaction() as connection:
            return self._add_dead_letter(connection, record)

    def fetch_events(
        self,
        experiment_key: str | None = None,
        experiment_version: int | None = None,
        *,
        order: str = "event",
    ) -> list[sqlite3.Row]:
        if order not in {"event", "receipt"}:
            raise ValueError("order must be event or receipt")
        clauses: list[str] = []
        values: list[object] = []
        if experiment_key is not None:
            clauses.append("experiment_key = ?")
            values.append(experiment_key)
        if experiment_version is not None:
            clauses.append("experiment_version = ?")
            values.append(experiment_version)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        ordering = "occurred_at, event_id" if order == "event" else "received_at, rowid"
        with self._lock:
            return self.connection.execute(
                f"SELECT * FROM events {where} ORDER BY {ordering}", values
            ).fetchall()

    def list_dead_letters(self, status: str = "pending") -> list[sqlite3.Row]:
        if status not in {"pending", "resolved", "all"}:
            raise ValueError("status must be pending, resolved, or all")
        query = "SELECT * FROM dead_letters"
        values: tuple[object, ...] = ()
        if status != "all":
            query += " WHERE status = ?"
            values = (status,)
        query += " ORDER BY dead_letter_id"
        with self._lock:
            return self.connection.execute(query, values).fetchall()

    def get_dead_letter(self, dead_letter_id: int) -> sqlite3.Row:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM dead_letters WHERE dead_letter_id = ?", (dead_letter_id,)
            ).fetchone()
        if row is None:
            raise KeyError(dead_letter_id)
        return row

    def mark_dead_letter_resolved(self, dead_letter_id: int, resolved_at: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE dead_letters
                SET status = 'resolved', resolved_at = ?, attempt_count = attempt_count + 1,
                    last_error = NULL
                WHERE dead_letter_id = ?
                """,
                (resolved_at, dead_letter_id),
            )

    def mark_dead_letter_attempt(self, dead_letter_id: int, error_message: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE dead_letters
                SET attempt_count = attempt_count + 1, last_error = ?
                WHERE dead_letter_id = ?
                """,
                (error_message, dead_letter_id),
            )

    def count_events(self, event_type: str | None = None) -> int:
        with self._lock:
            if event_type is None:
                row = self.connection.execute("SELECT COUNT(*) FROM events").fetchone()
            else:
                row = self.connection.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type = ?", (event_type,)
                ).fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.connection.close()
                self._closed = True

    def __enter__(self) -> "SQLiteEventRepository":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        # sqlite3 emits a ResourceWarning when a caller forgets to close. The
        # context-manager path is preferred, while this guard keeps short-lived
        # test and SDK stores from leaking the native connection at collection.
        try:
            self.close()
        except Exception:
            # Destructors can run during interpreter teardown after locks or the
            # sqlite module have already been finalized.
            pass


def _record_json(record: EventRecord) -> str:
    import json

    payload: Mapping[str, object] = {
        "event_id": record.event_id,
        "experiment_key": record.experiment_key,
        "experiment_version": record.experiment_version,
        "subject_id": record.subject_id,
        "event_type": record.event_type,
        "variant_key": record.variant_key,
        "metric": record.metric,
        "value": record.value,
        "occurred_at": record.occurred_at,
        "schema_version": record.schema_version,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
