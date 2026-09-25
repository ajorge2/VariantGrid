from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from uuid import uuid4

RUN_POSTGRES = os.environ.get("VARIANTGRID_RUN_POSTGRES_TESTS") == "1"
POSTGRES_DSN = os.environ.get(
    "VARIANTGRID_TEST_POSTGRES_DSN", "postgresql://localhost:5432/postgres"
)

if RUN_POSTGRES:
    import psycopg
    from psycopg import sql

    from variantgrid.events import Event, EventRejected, EventStore
    from variantgrid.postgres_event_storage import PostgresEventRepository

from tests.event_repository_contract import (
    EventRepositoryContractMixin,
    TIMESTAMP,
    record,
)


def new_schema() -> str:
    return f"variantgrid_test_{uuid4().hex}"


def drop_owned_schema(schema: str) -> None:
    if not schema.startswith("variantgrid_test_"):
        raise ValueError("refusing to drop a schema not created by this test suite")
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )


@unittest.skipUnless(RUN_POSTGRES, "set VARIANTGRID_RUN_POSTGRES_TESTS=1 for local Postgres")
class PostgresEventRepositoryContractTests(EventRepositoryContractMixin, unittest.TestCase):
    """Run the unchanged repository contract against a real Postgres schema."""

    def make_repository(self) -> "PostgresEventRepository":
        self.schema = new_schema()
        self.addCleanup(drop_owned_schema, self.schema)
        return PostgresEventRepository(POSTGRES_DSN, schema=self.schema)


@unittest.skipUnless(RUN_POSTGRES, "set VARIANTGRID_RUN_POSTGRES_TESTS=1 for local Postgres")
class PostgresEventRepositoryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = new_schema()
        self.addCleanup(drop_owned_schema, self.schema)
        self.repository = PostgresEventRepository(POSTGRES_DSN, schema=self.schema)
        self.addCleanup(self.repository.close)

    def test_concurrent_duplicate_delivery_across_independent_connections(self) -> None:
        repositories = [self.repository] + [
            PostgresEventRepository(POSTGRES_DSN, schema=self.schema, initialize=False)
            for _ in range(15)
        ]
        try:
            with ThreadPoolExecutor(max_workers=16) as pool:
                inserted = list(
                    pool.map(lambda repository: repository.insert_event(record("same")), repositories)
                )
            self.assertEqual(sum(inserted), 1)
            self.assertEqual(self.repository.count_events(), 1)
        finally:
            for repository in repositories[1:]:
                repository.close()

    def test_batch_retry_and_durable_reopen_recover_one_committed_result(self) -> None:
        events = (record("one"), record("two"))
        first = self.repository.insert_batch("retry-token", "stable-hash", events, (), TIMESTAMP)
        self.assertEqual((first.inserted, first.duplicates), (2, 0))
        self.repository.close()
        self.repository = PostgresEventRepository(
            POSTGRES_DSN, schema=self.schema, initialize=False
        )
        self.addCleanup(self.repository.close)
        recovered = self.repository.insert_batch(
            "retry-token", "stable-hash", events, (), TIMESTAMP
        )
        self.assertTrue(recovered.replayed)
        self.assertEqual((recovered.inserted, recovered.duplicates), (2, 0))
        self.assertEqual(self.repository.count_events(), 2)

    def test_dead_letter_replay_survives_repository_reopen(self) -> None:
        allowed: set[tuple[str, int]] = set()
        store = EventStore(
            repository=self.repository,
            version_validator=lambda key, version: (key, version) in allowed,
        )
        event = Event(
            "exposure-1",
            "checkout",
            1,
            "subject-1",
            "exposure",
            "control",
            occurred_at=TIMESTAMP,
        )
        with self.assertRaises(EventRejected):
            store.ingest(event)
        dead_letter_id = store.dead_letters()[0].dead_letter_id
        allowed.add(("checkout", 1))
        self.assertTrue(store.replay_dead_letter(dead_letter_id))
        store.close()
        self.repository = PostgresEventRepository(
            POSTGRES_DSN, schema=self.schema, initialize=False
        )
        self.addCleanup(self.repository.close)
        self.assertEqual(self.repository.count_events(), 1)
        resolved = self.repository.get_dead_letter(dead_letter_id)
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(int(resolved["attempt_count"]), 1)


if __name__ == "__main__":
    unittest.main()
