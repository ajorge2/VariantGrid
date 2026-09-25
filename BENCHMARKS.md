# VariantGrid benchmark report

Run timestamp: 2026-09-25T02:14:48Z  
Methodology version: 3.0  
Generated results: [`benchmarks/results.json`](benchmarks/results.json)

## Current clean-copy result

All predeclared local regression gates passed:

- 98 automated tests passed with `ResourceWarning` promoted to an error, including seven against PostgreSQL 17.
- 50/50, 80/20, and 90/10 policies each preserved 100% repeat-subject stickiness across 1,000,000 assignments.
- Maximum allocation drift was 0.0402, 0.0027, and 0.0176 percentage points respectively.
- Five one-million-assignment latency trials produced a 1.417 microsecond median sampled p95, a 1.416–1.958 microsecond trial range, and 705,834 median assignments per second.
- A/A false-positive rates were 5.29%, 5.13%, and 4.64% at 1%, 10%, and 50% baselines.
- Nominal-95% interval coverage was 94.71%, 94.87%, and 95.36% at those baselines.
- The injected two-point binary effect was recovered at 2.014 percentage points with 0.014-point absolute bias.

The generated replacement claim uses the current 1.417 microsecond median sampled p95. The exact historical 1.46 wording remains separately blocked because it is not the value produced by this clean-copy run; “under 1.5 microseconds” is supported.

## Predeclared regression gates

- 100% repeat-subject stickiness for every allocation policy.
- No more than 0.25 percentage-point allocation drift.
- No more than 25 microseconds median sampled p95 and at least 50,000 local assignments per second.
- False-positive rates from 4.5% to 5.5% at each A/A baseline.
- Nominal-95% interval coverage from 94% to 96% at each baseline.
- No more than 0.25 percentage-point absolute effect-recovery bias.

The performance limits are broad regression alarms, not resume wording. Exact public numbers must come from the generated claim manifests.

## Assignment protocol

Each allocation policy assigns one million stable subject IDs using SHA-256 over experiment key, immutable version, salt, algorithm version, policy version, and subject ID. The suite independently checks repeat assignments and deviation from declared weights.

Performance uses five trials of one million local assignments. Each trial samples one call in 100 and records p50, p95, p99, and throughput. The reported latency is the median trial p95 rather than the fastest result.

This is an in-process Python benchmark on the recorded arm64 macOS environment. It does not represent HTTP latency, deployed throughput, or a production SLO.

## Statistical protocol

The fixed-seed A/A matrix runs 10,000 simulations at 1%, 10%, and 50% baseline conversion, with 1,000 observations per variant. The 10% fixture deliberately preserves the historical seed and reproduces 5.13% false positives and 94.87% interval coverage.

Effect recovery runs 2,000 simulations with 3,000 observations per variant, a 10% control rate, and a 12% treatment rate. Additional unit tests cover binary and continuous positive, zero, and negative effects, power/MDE, SRM, guardrails, heavy-tail warnings, multiplicity warnings, and sequential monitoring.

## Integrity protocol

The adversarial suite covers exact and concurrent duplicates, missing exposure, event-time and receipt-time reordering, late events, inclusive attribution boundaries, retries after lost acknowledgements, dead-letter replay, payload collisions, malformed/unauthorized events, deterministic reconciliation, and cross-version isolation.

The identical repository contract passes against SQLite and PostgreSQL 17. PostgreSQL-specific tests also cover 16 independent connections racing on one event ID, retry recovery after durable reopen, and dead-letter replay after reopen. Test schemas are uniquely named and removed after execution. Sustained load, failover, multi-region behavior, tenant authorization, and production uptime remain unproven.

## Reproduce everything

```bash
PYTHONPATH=src python3 scripts/verify_release.py
```

That command stages a clean checkout-like copy, runs tests and benchmarks, and regenerates:

- `benchmarks/results.json`
- `artifacts/release_audit.json`
- `artifacts/release_manifest.json`
- `artifacts/test-results.txt`
- `artifacts/benchmark-output.txt`
- `artifacts/claims/*.json`

Do not edit generated evidence by hand.
