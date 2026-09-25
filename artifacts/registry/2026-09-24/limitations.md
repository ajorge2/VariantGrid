# Registry evidence limitations

- SQLite is a local reference registry, not proof of a highly available or
  distributed production control plane.
- Database-file access is trusted. Application-level authorization and audit
  identity are not implemented in this workstream.
- Migration rollback is a tested compatibility rollback that preserves
  additive columns; it is not a destructive reconstruction of the exact v1
  physical schema.
- Conditional variables intentionally may reference only earlier factors.
  Arbitrary dependency graphs are rejected instead of topologically inferred.
- Older hand-authored versions without stored factors replay their frozen state
  list but cannot reconstruct source variables that were never persisted.
- Local checks establish deterministic replay and immutability through the
  supported API and schema triggers. They do not prove multi-process behavior,
  backup recovery, disaster recovery, or sustained production uptime.
- Registry evidence covers only its portion of VG-1. Assignment, event
  ingestion, and release-evidence owners must independently sign off.
