# VariantGrid event ingestion and attribution

## Contract

VariantGrid preserves five event meanings rather than treating all telemetry as interchangeable:

| Type | Required fields | Meaning |
|---|---|---|
| `assignment` | experiment/version, subject, variant | State allocated to a subject; not proof of exposure |
| `exposure` | experiment/version, subject, variant | State the subject actually encountered |
| `goal` | experiment/version, subject, metric | Binary or incrementing outcome after exposure |
| `observation` | experiment/version, subject, metric, finite value | Continuous measurement after exposure |
| `guardrail` | experiment/version, subject, metric, finite value | Measurement whose regression can block a decision |

Every accepted event has a stable `event_id`, positive experiment version, schema version, timezone-aware event timestamp, server receipt timestamp, and immutable payload hash. The default schema version is `1`.

The service boundary can inject both a `version_validator` and `authorizer`. Unknown experiment versions and unauthorized writes are rejected before insertion and retained as inspectable dead letters. The unconfigured local store remains permissive for backwards-compatible unit tests; a service must configure both boundaries.

## Delivery and idempotency semantics

- Event delivery is **at least once**. A stable event ID gives exactly-once analytical effect for identical retries.
- Concurrent inserts serialize at the repository boundary. One identical event is inserted and all remaining deliveries are reported as duplicates.
- Reusing an event ID for a different immutable payload is a visible `event_id_collision`, not a duplicate.
- `ingest_batch(batch_id, events)` commits event writes, quarantines, and its response summary in one repository transaction.
- Retrying the same batch ID and ordered payload returns the committed summary with `replayed=true`. Reusing a batch ID for different content fails visibly.
- Invalid members of a mixed batch are quarantined while valid members commit. This is intentional partial acceptance, not an all-or-nothing validation policy.

## Attribution and reconciliation

Default readouts are exposure-gated. VariantGrid selects the subject's first same-version exposure by `(occurred_at, event_id)`. Goals, observations, and guardrails qualify only when their event time falls in the closed interval:

```text
exposure_time <= outcome_time <= exposure_time + attribution_window
```

Receipt order does not affect attribution. A goal received before a delayed exposure is attributed after reconciliation when its event time is causal. Pre-exposure outcomes, outcomes without exposure, outcomes outside the window, and outcomes from another experiment version are excluded.

`reconcile()` emits both receipt order and canonical event-time order, the exposure-gated counts, and a deterministic SHA-256 digest. Replaying the same accepted history in another receipt order produces the same digest. `export_events()` supplies stable row-shaped output for a future warehouse sink.

## Dead-letter operations

`dead_letters()` exposes payload, stable error code, error message, receipt time, status, replay attempts, and resolution time. `replay_dead_letter()` reruns schema, version, and authorization validation. A replay is resolved only after the event is inserted or confirmed as the identical existing event; failed attempts remain pending and auditable.

## Event-integrity evidence

Reproduce the event workstream with:

```bash
PYTHONPATH=src python3 -m unittest tests.test_event_ingestion -v
```

The current suite executes 15 tests covering the required adversarial matrix:

| Category | Fixture | Invariant | Observed local result |
|---|---|---|---|
| Exact duplicate | `test_exact_and_concurrent_duplicates_never_double_count` | Same immutable event changes counts once | One insertion; retry returns duplicate |
| Concurrent duplicate | same fixture, 64 deliveries / 12 workers | Concurrent retry cannot double count | Exactly one insertion |
| Conflicting ID | `test_event_id_reuse_for_different_payload_is_quarantined` | Different payload is not mislabeled duplicate | Rejected and quarantined |
| Goal before exposure receipt | `test_goal_received_before_exposure_reconciles_by_event_time` | Receipt order cannot alter causal event-time attribution | Receipt order differs; reconciled count is 1/1 |
| Missing exposure | `test_missing_exposure_and_pre_exposure_goal_are_excluded` | Assignment/outcome alone cannot enter default analysis | Orphan outcome excluded |
| Late exposure/outcome | `test_late_events_use_event_time_and_window_endpoint_is_inclusive` | Event time, not receipt time, controls attribution | Exact endpoint included; later event excluded |
| Reordered batch | `test_reordered_batches_reconcile_to_the_same_digest` | Equivalent history has one reconciliation result | Digests match across reversed delivery |
| Retry after timeout | `test_retry_after_timeout_recovers_the_committed_batch_result` | Lost acknowledgement cannot repeat analytical effect | Stored batch response recovered; two rows total |
| Cross-version event | `test_versions_are_validated_and_never_cross_attribution` | Version boundaries cannot leak | Unknown version rejected; V1 converter remains zero |
| Replay | `test_dead_letter_can_be_replayed_after_version_catalog_changes` | Corrected boundary state enables deterministic recovery | Pending letter resolves and inserts once |
| Malformed payload | `test_malformed_and_unauthorized_payloads_fail_visibly` | Schema failures are visible and inspectable | `invalid_schema` dead letter |
| Unauthorized payload | same fixture | Authorization failure occurs before insertion | `unauthorized` dead letter; zero events |
| Mixed batch | `test_invalid_batch_members_are_quarantined_without_losing_valid_members` | Partial acceptance is explicit | One accepted and one quarantined |
| Continuous/guardrail attribution | `test_observations_and_guardrails_are_exposure_gated` | Non-binary metrics use the same exposure boundary | Orphan excluded; qualifying values grouped by variant |
| Durable reopen | `test_file_store_reopens_with_identical_reconciliation` | Stored history recreates the same readout | Digest and counts unchanged after reopen |

The full repository regression command is:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`tests/event_repository_contract.py` is the executable adapter contract. Both
SQLite and Postgres run the same four contract cases. The Postgres integration
suite additionally proves concurrent duplicate delivery across 16 independent
connections, batch-result recovery after repository reopen, and dead-letter
replay durability.

The verified local Postgres target was PostgreSQL 17.9 at
`postgresql://localhost:5432/postgres`, authenticated by libpq as the local user
`ajorge`. Override the DSN rather than editing tests:

```bash
VARIANTGRID_RUN_POSTGRES_TESTS=1 \
VARIANTGRID_TEST_POSTGRES_DSN='postgresql://localhost:5432/postgres' \
PYTHONPATH=src .venv/bin/python -m unittest tests.test_postgres_event_repository -v
```

This command currently runs seven Postgres tests. Each case creates a unique
`variantgrid_test_<uuid>` schema and cleanup refuses to drop a schema outside
that prefix. The verified run left zero matching schemas behind.

## Persistence boundary and Postgres migration

`EventStore` depends on the `EventRepository` protocol. `SQLiteEventRepository`
is the deterministic embedded adapter. `PostgresEventRepository` is the durable
adapter and is available through the optional dependency:

```bash
pip install -e '.[postgres]'
```

The Postgres adapter provides:

1. unique constraints on `event_id` and `batch_id`;
2. payload-hash comparison when `ON CONFLICT` is reached;
3. one transaction around batch inserts, dead letters, and batch-result recording;
4. timezone-aware receipt time from the trusted ingestion-service clock;
5. transaction-scoped advisory locking for competing batch IDs;
6. indexed experiment/version/event-time attribution access; and
7. durable dead-letter state, attempts, and resolution records.

Initialization creates its named schema and tables when absent, but never drops
them. The database role therefore needs connect, schema creation/usage, and DDL/
DML rights for that schema. Production migration tooling must own schema changes;
the adapter's `CREATE TABLE IF NOT EXISTS` bootstrap is not an online migration
framework. Tenant/project authorization and managed online migrations remain
deployment requirements rather than capabilities of this repository class.

For SQLite, migrate by stopping writers, backing up the file, opening it with the
new adapter to create additive objects, running the event suite against a copy,
and then resuming writes. Rollback means restoring the backup before accepting
writes under the old schema.

## What this does not prove

The suite now proves the repository contract on PostgreSQL 17.9, including one
16-connection duplicate-delivery case, transaction-scoped batch retry, durable
reopen, and dead-letter replay. It does **not** prove multi-process or multi-region
behavior, production authorization, tenant isolation, sustained throughput,
availability, failover, disaster recovery, producer clock quality, warehouse
delivery, online migration safety, or zero data loss across infrastructure
failures. Those claims still require load, deployment, migration, failover, and
chaos evidence in the target production environment.
