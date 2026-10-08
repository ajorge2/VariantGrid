# VariantGrid methods and results

This index connects each system behavior to the test or benchmark that measures it and states the limits of the result.

## System behavior

| Area | Measured behavior | Primary artifacts | Boundary |
|---|---|---|---|
| Experiment registry | Typed canonical states, require/exclude combination and sequence rules, contradiction blocking, and immutable versioned configuration | `tests/test_registry.py`, `tests/test_dashboard.py` | Finite declared factor order, not an arbitrary constraint programming language |
| Assignment | Deterministic weighted allocation, version isolation, diagnostic output, and SDK/service parity | `tests/test_assignment.py`, `benchmarks/results.json` | Local evaluator, not network service latency or availability |
| Event ingestion | Idempotent, event-time-aware, exposure-gated telemetry across SQLite and PostgreSQL | `tests/test_event_ingestion.py`, `tests/test_event_repository_contract.py`, `tests/test_postgres_event_repository.py` | Local contention and reopen tests; no sustained load, failover, or multi-region measurement |
| Statistical inference | Binary and continuous estimands, planning, integrity checks, and recommendation suppression | `tests/test_inference_integrity.py`, `benchmarks/results.json` | Simulation and synthetic fixtures, not live causal impact |
| API and SDK | Versioned assignment and event interfaces, safe failures, and asynchronous exposure/outcome tracking | `tests/test_api_sdk.py`, `examples/reference_integration.py`, `examples/send_live_traffic.py` | Demonstration HTTP adapter and in-memory queue |
| Operator workflow | Event-derived plots, failure injection, and frozen decisions | `tests/test_dashboard.py`, `dashboard/README.md` | Process-local event store; some health and effect controls remain fixtures |

## Measured results

- Three million-assignment policies retained 100% repeat-subject stickiness.
- Maximum allocation drift was no greater than 0.0402 percentage points.
- The median sampled p95 local assignment latency was 1.458 microseconds.
- Ten thousand A/A simulations produced a 5.13% false-positive rate and 94.87% interval coverage at a nominal 5% level.
- Duplicate, missing, late, reordered, and cross-version event cases passed the local integrity suite.

The latency result is hardware-sensitive and measures in-process Python execution rather than service latency. The calibration results use fixed-seed simulations. The event tests establish local SQLite semantics rather than distributed durability.

## Applied threshold decision

The CodeArchitect study replays a completed blinded human review through VariantGrid. It registers two threshold policies, validates the frozen inputs, recomputes the coverage and precision measures, applies the pre-registered decision rules, and stores the result.

- Evaluation population: 151 components from 132 repository families excluded from taxonomy fitting.
- Primary metric: accepted coverage increased from 11.9% to 23.2%, clearing the 20% target.
- Quality constraints: strict precision was 68.6% against an 80% minimum and fell 9.2 percentage points against a maximum permitted five-point decline.
- Decision: retain the `0.72` threshold rather than lower it to `0.62`.

This is a paired offline policy evaluation on the same components, not a randomized A/B test. It does not measure developer adoption or downstream product impact.

## Reproduction thresholds

- 100% repeat-subject stickiness for each million-assignment policy.
- Maximum allocation drift of 0.25 percentage points.
- Median sampled p95 local assignment latency no greater than 25 microseconds and median throughput of at least 50,000 assignments per second.
- A/A false-positive rate between 4.5% and 5.5% at each baseline.
- Nominal-95% interval coverage between 94% and 96% at each baseline.
- Injected-effect absolute bias no greater than 0.25 percentage points.
- Complete automated suite passes with `ResourceWarning` promoted to an error.

Run `PYTHONPATH=src python3 scripts/verify_release.py` to execute the clean-copy test, benchmark, and integrity suites. The generated JSON records the environment, thresholds, observed values, source fingerprint, and limitations for each run.
