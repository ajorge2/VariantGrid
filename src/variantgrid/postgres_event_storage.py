from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator, Mapping, Sequence

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
except ImportError as error:  # pragma: no cover - exercised only without the optional extra
    raise ImportError(
        "PostgresEventRepository requires the optional 'postgres' dependency; "
        "install VariantGrid with `pip install -e '.[postgres]'`"
    ) from error

from .event_storage import (
    BatchIdCollisionError,
    BatchWriteResult,
    DeadLetterRecord,
    EventIdCollisionError,
    EventRecord,
)


class PostgresEventRepository:
    """Transactional Postgres implementation of the EventRepository contract.

    The adapter owns tables inside the explicitly supplied schema, but never
    drops that schema. Tests and deployment tooling remain responsible for the
    lifecycle of schemas they create.
    """

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "variantgrid",
        initialize: bool = True,
    ) -> None:
        if not schema or "\x00" in schema:
            raise ValueError("schema must be a non-empty PostgreSQL identifier")
        self.dsn = dsn
        self.schema = schema
        # Autocommit keeps read helpers from leaving an implicit transaction
        # open; write methods still enter explicit atomic transactions below.
        self.connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self._lock = RLock()
        self._closed = False
        if initialize:
            self.initialize()

    def _table(self, name: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(name))

    def initialize(self) -> None:
        events = self._table("events")
        batches = self._table("ingestion_batches")
        dead_letters = self._table("dead_letters")
        with self._transaction() as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
            )
            connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        ingest_sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
                        event_id TEXT PRIMARY KEY,
                        experiment_key TEXT NOT NULL,
                        experiment_version INTEGER NOT NULL CHECK(experiment_version > 0),
                        subject_id TEXT NOT NULL,
                        event_type TEXT NOT NULL CHECK(event_type IN ('assignment','exposure','goal','observation','guardrail')),
                        variant_key TEXT,
                        metric TEXT,
                        value DOUBLE PRECISION,
                        occurred_at TIMESTAMPTZ NOT NULL,
                        received_at TIMESTAMPTZ NOT NULL,
                        schema_version INTEGER NOT NULL,
                        payload_hash TEXT NOT NULL
                    )
                    """
                ).format(events)
            )
            connection.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} "
                    "(experiment_key, experiment_version, event_type, metric, subject_id)"
                ).format(sql.Identifier("event_lookup"), events)
            )
            connection.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} "
                    "(experiment_key, experiment_version, occurred_at, event_id)"
                ).format(sql.Identifier("event_time_lookup"), events)
            )
            connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        batch_id TEXT PRIMARY KEY,
                        batch_hash TEXT NOT NULL,
                        inserted_count INTEGER NOT NULL,
                        duplicate_count INTEGER NOT NULL,
                        dead_letter_count INTEGER NOT NULL,
                        received_at TIMESTAMPTZ NOT NULL
                    )
                    """
                ).format(batches)
            )
            connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        dead_letter_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        error_code TEXT NOT NULL,
                        error_message TEXT NOT NULL,
                        received_at TIMESTAMPTZ NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved')),
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        resolved_at TIMESTAMPTZ
                    )
                    """
                ).format(dead_letters)
            )

    @contextmanager
    def _transaction(self) -> Iterator["psycopg.Connection[Mapping[str, object]]"]:
        with self._lock:
            with self.connection.transaction():
                yield self.connection

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

    def _insert_event(self, connection: "psycopg.Connection", record: EventRecord) -> bool:
        cursor = connection.execute(
            sql.SQL(
                """
                INSERT INTO {} (
                    event_id, experiment_key, experiment_version, subject_id,
                    event_type, variant_key, metric, value, occurred_at, received_at,
                    schema_version, payload_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """
            ).format(self._table("events")),
            self._event_values(record),
        )
        if cursor.fetchone() is not None:
            return True
        existing = connection.execute(
            sql.SQL("SELECT payload_hash FROM {} WHERE event_id = %s").format(
                self._table("events")
            ),
            (record.event_id,),
        ).fetchone()
        if existing is None:
            raise RuntimeError("conflicting event disappeared during insertion")
        if existing["payload_hash"] == record.payload_hash:
            return False
        raise EventIdCollisionError(
            f"event_id {record.event_id!r} already belongs to a different payload"
        )

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
            # A transaction-scoped advisory lock makes competing first delivery
            # of the same batch deterministic across repository connections.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (batch_id,)
            )
            existing = connection.execute(
                sql.SQL("SELECT * FROM {} WHERE batch_id = %s").format(
                    self._table("ingestion_batches")
                ),
                (batch_id,),
            ).fetchone()
            if existing is not None:
                if existing["batch_hash"] != batch_hash:
                    raise BatchIdCollisionError(
                        f"batch_id {batch_id!r} already belongs to a different payload"
                    )
                return BatchWriteResult(
                    int(existing["inserted_count"]),
                    int(existing["duplicate_count"]),
                    int(existing["dead_letter_count"]),
                    replayed=True,
                )

            inserted = 0
            duplicates = 0
            generated = list(dead_letters)
            for record in records:
                try:
                    if self._insert_event(connection, record):
                        inserted += 1
                    else:
                        duplicates += 1
                except EventIdCollisionError as error:
                    generated.append(
                        DeadLetterRecord(
                            payload_json=_record_json(record),
                            error_code="event_id_collision",
                            error_message=str(error),
                            received_at=received_at,
                        )
                    )
            for letter in generated:
                self._add_dead_letter(connection, letter)
            result = BatchWriteResult(inserted, duplicates, len(generated))
            connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (
                        batch_id, batch_hash, inserted_count, duplicate_count,
                        dead_letter_count, received_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """
                ).format(self._table("ingestion_batches")),
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

    def _add_dead_letter(self, connection: "psycopg.Connection", record: DeadLetterRecord) -> int:
        row = connection.execute(
            sql.SQL(
                """
                INSERT INTO {} (payload_json, error_code, error_message, received_at)
                VALUES (%s, %s, %s, %s)
                RETURNING dead_letter_id
                """
            ).format(self._table("dead_letters")),
            (record.payload_json, record.error_code, record.error_message, record.received_at),
        ).fetchone()
        if row is None:
            raise RuntimeError("dead letter insert did not return an identifier")
        return int(row["dead_letter_id"])

    def add_dead_letter(self, record: DeadLetterRecord) -> int:
        with self._transaction() as connection:
            return self._add_dead_letter(connection, record)

    def fetch_events(
        self,
        experiment_key: str | None = None,
        experiment_version: int | None = None,
        *,
        order: str = "event",
    ) -> list[Mapping[str, object]]:
        if order not in {"event", "receipt"}:
            raise ValueError("order must be event or receipt")
        clauses: list[sql.SQL] = []
        values: list[object] = []
        if experiment_key is not None:
            clauses.append(sql.SQL("experiment_key = %s"))
            values.append(experiment_key)
        if experiment_version is not None:
            clauses.append(sql.SQL("experiment_version = %s"))
            values.append(experiment_version)
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("")
        ordering = (
            sql.SQL("occurred_at, event_id")
            if order == "event"
            else sql.SQL("received_at, ingest_sequence")
        )
        query = sql.SQL("SELECT * FROM {}{} ORDER BY {}").format(
            self._table("events"), where, ordering
        )
        with self._lock:
            return list(self.connection.execute(query, values).fetchall())

    def list_dead_letters(self, status: str = "pending") -> list[Mapping[str, object]]:
        if status not in {"pending", "resolved", "all"}:
            raise ValueError("status must be pending, resolved, or all")
        query = sql.SQL("SELECT * FROM {} ").format(self._table("dead_letters"))
        values: tuple[object, ...] = ()
        if status != "all":
            query += sql.SQL("WHERE status = %s ")
            values = (status,)
        query += sql.SQL("ORDER BY dead_letter_id")
        with self._lock:
            return list(self.connection.execute(query, values).fetchall())

    def get_dead_letter(self, dead_letter_id: int) -> Mapping[str, object]:
        with self._lock:
            row = self.connection.execute(
                sql.SQL("SELECT * FROM {} WHERE dead_letter_id = %s").format(
                    self._table("dead_letters")
                ),
                (dead_letter_id,),
            ).fetchone()
        if row is None:
            raise KeyError(dead_letter_id)
        return row

    def mark_dead_letter_resolved(self, dead_letter_id: int, resolved_at: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                sql.SQL(
                    """
                    UPDATE {} SET status = 'resolved', resolved_at = %s,
                        attempt_count = attempt_count + 1, last_error = NULL
                    WHERE dead_letter_id = %s
                    """
                ).format(self._table("dead_letters")),
                (resolved_at, dead_letter_id),
            )

    def mark_dead_letter_attempt(self, dead_letter_id: int, error_message: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                sql.SQL(
                    """
                    UPDATE {} SET attempt_count = attempt_count + 1, last_error = %s
                    WHERE dead_letter_id = %s
                    """
                ).format(self._table("dead_letters")),
                (error_message, dead_letter_id),
            )

    def count_events(self, event_type: str | None = None) -> int:
        query = sql.SQL("SELECT COUNT(*) AS count FROM {}").format(self._table("events"))
        values: tuple[object, ...] = ()
        if event_type is not None:
            query += sql.SQL(" WHERE event_type = %s")
            values = (event_type,)
        with self._lock:
            row = self.connection.execute(query, values).fetchone()
        return int(row["count"] if row is not None else 0)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.connection.close()
                self._closed = True

    def __enter__(self) -> "PostgresEventRepository":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _record_json(record: EventRecord) -> str:
    import json

    return json.dumps(
        {
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
        },
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = ["PostgresEventRepository"]
