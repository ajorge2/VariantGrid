# VariantGrid

VariantGrid is a local, evidence-first experimentation platform for moving from a product hypothesis to an auditable decision. It is built around the failure modes that make experiment readouts unreliable: unstable assignment, invalid multivariate combinations, duplicate events, missing exposure, reordered delivery, sample-ratio mismatch, unsafe peeking, and experiment-version leakage.

## What is implemented

- **CAP-A — experiment registry:** typed integer, float, boolean, and enum variables; conditional variables; constraint-valid canonical states; persistent immutable launched versions; lifecycle transitions; metric, guardrail, and decision records.
- **CAP-B — assignment:** deterministic SHA-256 weighted allocation with experiment, version, salt, algorithm, and policy isolation; eligibility and explicit fallbacks; bucket/interval diagnostics; direct/service/SDK parity fixtures.
- **CAP-C — event semantics:** strict assignment, exposure, goal, observation, and guardrail schemas; stable event identifiers; local SQLite durability; batch retry safety; dead letters; deterministic event-time reconciliation; exposure-gated attribution.
- **CAP-D — decision integrity:** binary and continuous effects, intervals, tests, power/MDE, SRM detection, guardrails, sequential-monitoring and multiplicity warnings, and suppression of recommendations when integrity gates fail.
- **CAP-E — integration:** a versioned local HTTP contract and Python SDK with `get`, `goal`, `increment`, and `observe`; bounded timeouts; cache visibility; safe defaults; asynchronous tracking; and retry idempotency.
- **CAP-F — operator sandbox:** constrained-state preview, launch review, data health before readouts, failure injection, version history, immutable evidence snapshots, lifecycle controls, and rollback.
- **CAP-G — evidence gates:** clean-copy verification, generated claim manifests, fixed-seed statistical calibration, repeated latency trials, adversarial integrity tests, and CI artifact retention.

The IDs above map to owners, tests, limitations, and generated evidence in [docs/EVIDENCE.md](docs/EVIDENCE.md). Architecture and data flow are documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Verify everything with one command

```bash
PYTHONPATH=src python3 scripts/verify_release.py
```

The command stages a clean checkout-like temporary copy, treats resource leaks as test failures, runs the full test suite and benchmark, applies predeclared regression gates, and copies these generated artifacts back:

- `benchmarks/results.json`
- `artifacts/release_audit.json`
- `artifacts/release_manifest.json`
- `artifacts/test-results.txt`
- `artifacts/benchmark-output.txt`
- one manifest per protected claim under `artifacts/claims/`

CI runs the same command. Resume wording is publishable only when its generated claim manifest says `approved` and [CLAIMS.md](CLAIMS.md) agrees.

## Run the product path

Reference SDK integration:

```bash
PYTHONPATH=src python3 examples/reference_integration.py
```

Operator sandbox:

```bash
PYTHONPATH=src python3 -m variantgrid.dashboard --port 8766
```

Open `http://127.0.0.1:8766`, then follow the [two-minute demo](docs/DEMO.md). The dashboard is a local sandbox, not a production control plane.

## Current evidence boundary

Fresh local evidence supports the typed registry, deterministic assignment, local idempotent event semantics, calibrated simulations, guarded decisions, SDK/API contract, and operator workflow. The historical **1.46 microsecond** assignment-latency wording remains blocked unless a fresh repeated run reproduces it; use the generated `VG-2R` wording instead.

VariantGrid now exercises its real Postgres adapter against PostgreSQL 17, including transactional contention and durable reopen cases. It still does **not** prove sustained distributed load, failover, deployed authentication, multi-region operation, production uptime, customer adoption, revenue lift, or live causal impact. The exact boundary is generated in `artifacts/release_audit.json` and summarized in [docs/WORKSTREAM_AUDIT.md](docs/WORKSTREAM_AUDIT.md).

## Project guide

- [Engineering build plan](ENGINEERING_BUILD_PLAN.md)
- [Architecture and data flow](docs/ARCHITECTURE.md)
- [Public evidence map](docs/EVIDENCE.md)
- [Two-minute demo](docs/DEMO.md)
- [Case study](docs/CASE_STUDY.md)
- [Benchmark methodology](BENCHMARKS.md)
- [Protected claim registry](CLAIMS.md)
- [Workstream audit](docs/WORKSTREAM_AUDIT.md)
- [Team routing](RUN_TEAM.md)
