# VariantGrid experiment registry

The local registry is the reproducible configuration source of truth for a
VariantGrid experiment. It persists one canonical JSON snapshot per version
and derives normalized metric and guardrail records from that snapshot.

## Model and state-generation contract

`Factor` supports finite integer, float, boolean, and enum values. A factor may
declare `active_when`; all referenced factors must appear earlier in the
definition and the factor is omitted when its condition is false. Forward and
unknown dependencies fail validation, eliminating ambiguous or cyclic
conditional semantics.

`generate_state_space` returns both accepted states and rejected combinations.
Every rejected combination includes the human-readable reason from each failed
constraint. Accepted and rejected output is sorted by canonical JSON. State IDs
are the first 20 hexadecimal characters of SHA-256 over the canonical,
key-sorted JSON values, so dictionary insertion order cannot change identity.

An `Experiment` stores the source factors and constraints alongside its frozen
states. `Experiment.replay_states()` regenerates the state space and preserves
weights by state ID. Registry reads fail if the regenerated state identity or
order differs from the frozen assignment input.

## Persisted data

SQLite schema version 2 stores:

- experiment identity and hypothesis;
- immutable experiment-version configuration, lifecycle, checksum, schema
  version, creation time, and first launch time;
- normalized primary/secondary metric definitions;
- normalized guardrail thresholds; and
- append-only decision text, evidence snapshots, and timestamps.

The canonical version snapshot freezes factors, conditional semantics,
constraints and reasons, state IDs and weights, eligibility, salt, assignment
algorithm and policy versions, metrics, guardrails, attribution window,
stopping rule, sample requirement, and statistical method.

## Lifecycle and edits

Allowed transitions are:

```text
draft -> running -> paused -> running
                 -> stopped -> archived
draft -> archived
paused -> stopped
```

All other transitions fail. Draft configuration may be replaced. The first
transition to running records `launched_at`; after that, configuration changes
are rejected by both the registry API and a database trigger. Material changes
must use `clone_version` or `clone_with_edits`, producing a newer draft while
leaving the launched source byte-for-byte unchanged.

Decision records require a running, paused, or stopped version and a non-empty
evidence snapshot. They are append-only and protected from SQL update/delete by
database triggers.

## Migration and rollback

`registry_migrations.migrate_registry` upgrades an unversioned or schema-v1
database transactionally to schema v2 and backfills checksums plus normalized
records when `ExperimentRegistry` opens it. `rollback_registry` supports v2 to
v1 rollback: it removes v2 derivative tables and triggers but intentionally
leaves additive columns inert, retaining the canonical JSON without a lossy
table rewrite. Reapplying v2 reconstructs derivatives from that JSON.

Before migrating a production-like database, take an external filesystem or
database backup and test restoration. This local migration path has not been
validated under distributed writers or production traffic.

## Assignment and API compatibility

`RegisteredVersion.experiment` remains the input consumed by
`AssignmentEngine` and `VariantGridAPI`. Existing constructors remain valid;
new metadata fields are additive defaults. The registry test suite verifies
that file-backed replay gives the same ordered state IDs and 1,000 deterministic
assignment results, while the full API/SDK parity suite verifies the shared
contract.

## Verification

```bash
PYTHONPATH=src python3 -m unittest tests.test_registry -v
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The registry tests cover all supported variable types, conditional variables,
rejected-state reasons, mapping-order independence, persistent replay,
checksums, immutable launch configuration, clone-on-edit, lifecycle rejection,
metrics, guardrails, decisions, and migration rollback/reapply.

