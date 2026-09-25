# Public evidence map

This page is the human-readable index. The generated source of truth is `artifacts/release_audit.json`, with one machine-readable manifest per protected claim under `artifacts/claims/`.

## Capability claims

| ID | Public capability | Owner | Primary evidence | Limitation |
|---|---|---|---|---|
| CAP-A | Typed, constrained canonical states and immutable versioned registry | Registry engineer | `tests/test_registry.py` | Local SQLite persistence, not distributed control-plane durability |
| CAP-B | Deterministic weighted, isolated, diagnosable assignment with SDK/service parity | Assignment engineer | `tests/test_assignment.py`, `benchmarks/results.json` | Local evaluator, not network service latency or availability |
| CAP-C | Idempotent, event-time-aware, exposure-gated telemetry semantics across SQLite and PostgreSQL | Event-ingestion engineer | `tests/test_event_ingestion.py`, `tests/test_event_repository_contract.py`, `tests/test_postgres_event_repository.py` | Real local Postgres contention/reopen evidence; no sustained load, failover, or multi-region proof |
| CAP-D | Binary/continuous inference, planning, integrity gates, and recommendation suppression | Inference-integrity engineer | `tests/test_inference_integrity.py`, `benchmarks/results.json` | Simulation and synthetic fixtures, not live causal impact |
| CAP-E | Versioned local API and Python SDK with safe failures and async tracking | API/SDK engineer | `tests/test_api_sdk.py`, `examples/reference_integration.py` | Demonstration HTTP adapter and in-memory queue |
| CAP-F | Auditable local operator workflow with failure injection and immutable decisions | Dashboard engineer | `tests/test_dashboard.py`, `dashboard/README.md` | Process-local sandbox with explicit fixtures |
| CAP-G | Clean-copy release gate, generated artifacts, and CI retention | Evidence/release engineer | `scripts/verify_release.py`, `.github/workflows/ci.yml` | CI is a regression gate, not production observability |

## Protected claims

| ID | Owner | Status source | Reproduce |
|---|---|---|---|
| VG-1 | Registry, assignment, and ingestion engineers | `artifacts/claims/VG-1.json` | `PYTHONPATH=src python3 scripts/verify_release.py` |
| VG-2 | Assignment engineer | `artifacts/claims/VG-2.json` | same command; exact historical wording must reproduce every conjunct |
| VG-2R | Assignment engineer | `artifacts/claims/VG-2R.json` | same command; wording is regenerated from current measurements |
| VG-3 | Inference-integrity and ingestion engineers | `artifacts/claims/VG-3.json` | same command |

## Predeclared gates

- 100% repeat-subject stickiness for each million-assignment policy.
- Maximum allocation drift no greater than 0.25 percentage points.
- Median sampled p95 local assignment latency no greater than 25 microseconds and median throughput at least 50,000 assignments/second. These are broad CI regression limits, not the historical 1.46 microsecond claim.
- A/A false-positive rate between 4.5% and 5.5% at each baseline.
- Nominal-95% interval coverage between 94% and 96% at each baseline.
- Injected-effect absolute bias no greater than 0.25 percentage points.
- Complete automated suite passes with `ResourceWarning` promoted to an error.

## Evidence integrity

Benchmark JSON and claim manifests are generated, not edited as prose. Every manifest records the exact wording, owner, independent signer, source fingerprint, reproduction command, seed or fixture, environment, thresholds, observed value, limitation, and status. A parent-repository commit may be listed for provenance, but the source fingerprint is authoritative while this project remains an uncommitted directory inside a larger workspace.
