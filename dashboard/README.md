# VariantGrid operator sandbox

This local dashboard makes the trustworthy-experiment workflow inspectable
without requiring database access or third-party packages. It is a product
sandbox, not a production control plane.

## Run it

From the repository root:

```bash
PYTHONPATH=src python3 -m variantgrid.dashboard --port 8766
```

Then open `http://127.0.0.1:8766`.

## Two-minute walkthrough

1. Read the prefilled hypothesis, primary metric, guardrail threshold,
   eligibility rule, allocation, and stopping rule.
2. Inspect the typed variable JSON and the constraint. Select **Validate and
   preview states**. The invalid `blank + guided tour` combination remains in
   the table with its rejection reason.
3. Review the baseline, target effect, required sample size, and implied MDE.
4. Try launching without the review checkbox. The launch is blocked. Confirm
   the review and launch immutable version 1.
5. Confirm that Data health appears before Effect and uncertainty. The healthy
   fixture shows denominators, unit, time range, experiment version, effect,
   interval, p-value, and recommendation.
6. Select **Inject missing exposure** (or SRM, delay, guardrail breach, version
   mismatch, stale config, or underpowered). The effect is withheld and the
   decision action becomes unavailable.
7. Clear the fixture and record a decision. The decision log contains a frozen
   evidence payload rather than a link to mutable current state.
8. Pause/resume or clone version 1. Launch the cloned version to exercise
   rollback while retaining both immutable configurations.

## Operator guarantees demonstrated

- Missing decision inputs, invalid variables, and an empty valid-state space
  block launch.
- A launch requires explicit review and stores canonical configuration JSON.
- A launched configuration cannot be edited; changes require cloning.
- Data health gates effect rendering and decision recommendations.
- Decision records contain the configuration, data-health fixture, readout,
  and recommendation that existed at decision time.
- Pause, stop, kill, rollback, and clone actions do not rewrite launched
  configuration payloads.

## Test it

```bash
PYTHONPATH=src python3 -m unittest tests.test_dashboard -v
```

The workflow tests cover valid and rejected states, missing inputs, empty state
space, immutable launch review, data-health ordering, seven injected integrity
failures, chart labels, evidence snapshots, and lifecycle controls.

## Rollback

The dashboard is isolated to `src/variantgrid/dashboard.py`,
`tests/test_dashboard.py`, and `dashboard/`. Removing those paths removes the
sandbox without changing the registry, assignment, ingestion, or statistical
modules.

## Honest limitations

- State is process-local and is lost when the server stops.
- There is no authentication, authorization, CSRF protection, multi-operator
  concurrency control, or durable audit store. Bind only to localhost.
- Health and metric values are explicit fixtures; this surface does not prove
  production ingestion or distributed failure recovery.
- The chart is an accessible HTML readout rather than a full visualization
  system.
- The dashboard demonstrates correct blocking behavior against existing core
  interfaces. Rendering a check does not independently prove the backend
  guarantee behind it.

