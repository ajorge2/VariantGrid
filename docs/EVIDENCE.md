# Public evidence map

This page is the human-readable index. The generated source of truth is `artifacts/release_audit.json`, with one machine-readable manifest per protected claim under `artifacts/claims/`.

## Capability claims

| ID | Public capability | Owner | Primary evidence | Limitation |
|---|---|---|---|---|
| CAP-A | Typed canonical states, require/exclude combination and sequence rules, contradiction blocking, and immutable versioned registry | Registry engineer | `tests/test_registry.py`, `tests/test_dashboard.py` | Finite declared factor order, not an arbitrary constraint programming language |
| CAP-B | Deterministic weighted, isolated, diagnosable assignment with SDK/service parity | Assignment engineer | `tests/test_assignment.py`, `benchmarks/results.json` | Local evaluator, not network service latency or availability |
| CAP-C | Idempotent, event-time-aware, exposure-gated telemetry semantics across SQLite and PostgreSQL | Event-ingestion engineer | `tests/test_event_ingestion.py`, `tests/test_event_repository_contract.py`, `tests/test_postgres_event_repository.py` | Real local Postgres contention/reopen evidence; no sustained load, failover, or multi-region proof |
| CAP-D | Binary/continuous inference, planning, integrity gates, and recommendation suppression | Inference-integrity engineer | `tests/test_inference_integrity.py`, `benchmarks/results.json` | Simulation and synthetic fixtures, not live causal impact |
| CAP-E | Versioned HTTP API and Python SDK with idempotent assignment events, safe failures, and async exposure/outcome tracking | API/SDK engineer | `tests/test_api_sdk.py`, `examples/reference_integration.py`, `examples/send_live_traffic.py` | Demonstration HTTP adapter and in-memory queue |
| CAP-F | Auditable local operator workflow with live event-derived plots, failure injection, and immutable decisions | Dashboard engineer | `tests/test_dashboard.py`, `dashboard/README.md` | Process-local event store; health and decision-effect controls remain fixtures |
| CAP-G | Clean-copy release gate, generated artifacts, and CI retention | Evidence/release engineer | `scripts/verify_release.py`, `.github/workflows/ci.yml` | CI is a regression gate, not production observability |

## Protected claims

| ID | Owner | Status source | Reproduce |
|---|---|---|---|
| VG-1 | Registry, assignment, and ingestion engineers | `artifacts/claims/VG-1.json` | `PYTHONPATH=src python3 scripts/verify_release.py` |
| VG-2 | Assignment engineer | `artifacts/claims/VG-2.json` | same command; exact historical wording must reproduce every conjunct |
| VG-2R | Assignment engineer | `artifacts/claims/VG-2R.json` | same command; wording is regenerated from current measurements |
| VG-3 | Inference-integrity and ingestion engineers | `artifacts/claims/VG-3.json` | same command |
| VG-4 | Offline policy evaluation | `studies/codearchitect-threshold-evaluation/decision-snapshot.json` | `PYTHONPATH=src python3 studies/codearchitect-threshold-evaluation/run_evaluation.py --replace` |

## Completed applied decision

`VG-4` replays CodeArchitect's completed blinded human review through
VariantGrid. The platform registers the two threshold policies, validates and
hashes the frozen evidence inputs, recomputes every reported coverage and
precision measure, applies three pre-registered gates, and stores the no-ship
result in its decision registry.

- Evaluation population: 151 components from 132 repository families excluded
  from taxonomy fitting.
- Primary metric: accepted coverage, which moves from 11.9% to 23.2% and clears
  the 20% bar.
- Quality guardrails: candidate strict precision is 68.6% against an 80% floor
  and declines 9.2 percentage points against a maximum allowed five-point drop.
- Decision: no-ship lowering the acceptance threshold from `0.72` to `0.62`.

This is a paired offline policy evaluation on the same components rather than a
randomized A/B test. Its human labels are completed evidence; it does not establish
developer adoption or downstream product impact. Unlike `VG-1` through `VG-3`,
`VG-4` is a reproduced applied-study artifact rather than a clean-copy release
claim with an independent signer.

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
