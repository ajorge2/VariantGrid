# VariantGrid

VariantGrid is a local, evidence-first experimentation platform for moving from a product hypothesis to an auditable decision. It is built around the failure modes that make experiment readouts unreliable: unstable assignment, invalid multivariate combinations, duplicate events, missing exposure, reordered delivery, sample-ratio mismatch, unsafe peeking, and experiment-version leakage.

## What is implemented

- **CAP-A — experiment registry:** typed integer, float, boolean, and enum variables; conditional variables; require/exclude rules for factor combinations and declared sequences; contradiction checks; constraint-valid canonical states; persistent immutable launched versions; lifecycle transitions; metric, guardrail, and decision records.
- **CAP-B — assignment:** deterministic SHA-256 weighted allocation with experiment, version, salt, algorithm, and policy isolation; eligibility and explicit fallbacks; bucket/interval diagnostics; direct/service/SDK parity fixtures.
- **CAP-C — event semantics:** strict assignment, exposure, goal, observation, and guardrail schemas; stable event identifiers; local SQLite durability; batch retry safety; dead letters; deterministic event-time reconciliation; exposure-gated attribution.
- **CAP-D — decision integrity:** binary and continuous effects, intervals, tests, power/MDE, SRM detection, guardrails, sequential-monitoring and multiplicity warnings, and suppression of recommendations when integrity gates fail.
- **CAP-E — integration:** a versioned HTTP contract and Python SDK with `get`, `goal`, `increment`, and `observe`; idempotent assignment records; bounded timeouts; cache visibility; safe defaults; asynchronous tracking; and retry idempotency.
- **CAP-F — operator sandbox:** constrained-state preview, rule validation, launch review, live event-derived traffic and conversion plots, data health before decision fixtures, failure injection, version history, immutable evidence snapshots, lifecycle controls, and rollback.
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

After launching version 1, either open **Instrumented product** from the Results
step or send traffic from a separate product process over the public SDK/API
contract:

```bash
PYTHONPATH=src python3 examples/send_live_traffic.py --subjects 20
```

Assignment, exposure, and outcome events are reconciled into the dashboard's
traffic and conversion plots every two seconds. This demonstrates the deployed
integration boundary; it is not evidence of customer production traffic.

## Replay the completed CodeArchitect decision

VariantGrid now has a completed applied policy evaluation in addition to its
synthetic and adversarial validation. It consumes CodeArchitect's frozen blinded
human-review evidence, registers the `0.72` and `0.62` model policies, recomputes
coverage and strict precision, applies the pre-registered gates, and stores the
result as an immutable VariantGrid decision record:

```bash
PYTHONPATH=src python3 studies/codearchitect-threshold-evaluation/run_evaluation.py --replace
```

The replay returns **no-ship**. Accepted coverage rises from 11.9% to 23.2% and
clears the 20% bar, while strict human-reviewed precision falls to 68.6%, below
the 80% floor and 9.2 percentage points below the conservative policy. See the
[study protocol and boundary](studies/codearchitect-threshold-evaluation/README.md)
and generated
[decision snapshot](studies/codearchitect-threshold-evaluation/decision-snapshot.json).

This is a paired offline model-policy evaluation on the same 151 components, not
a randomized user experiment or evidence of downstream developer outcomes.

## Deploy the portfolio demo on Northflank

The repository includes a Heroku-compatible `Procfile`, Python buildpack detection via `requirements.txt`, and a pinned Python version. In Northflank:

1. Create a combined service from this repository and choose **Buildpack**.
2. Select **`heroku/builder:24`** as the buildpack stack and keep the build context at the repository root.
3. Add a public **HTTP** port with internal port **`8080`**.
4. Optionally configure an HTTP health check at **`/healthz`** on port **`8080`**.

The web process binds to `0.0.0.0` and uses the platform's `PORT` variable when present, falling back to `8080`. This remains an ephemeral portfolio sandbox: state is kept in memory, resets when the container restarts, and is not an authenticated production control plane.

## Current evidence boundary

Fresh local evidence supports the typed registry, deterministic assignment, local idempotent event semantics, calibrated simulations, guarded decisions, SDK/API contract, operator workflow, and the completed CodeArchitect offline policy replay. The historical **1.46 microsecond** assignment-latency wording remains blocked unless a fresh repeated run reproduces it; use the generated `VG-2R` wording instead.

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
