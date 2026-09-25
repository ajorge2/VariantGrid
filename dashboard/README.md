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

1. In **Assignment Lab**, assign one subject twice. Inspect the identical state,
   SHA-256 bucket interval, algorithm and policy versions, and isolated
   comparison-version namespace.
2. Scan the approved generated 50/50, 80/20, and 90/10 million-assignment rows
   plus the repeated-trial local latency summary. The dashboard loads these
   artifacts; it does not rerun the benchmark.
3. In **Event Integrity Lab**, inspect the expected invariant and observed real
   local-pipeline result for duplicate, missing-exposure, late, reordered, and
   cross-version events.
4. In **Validation Evidence**, connect VG-1, VG-2R, and VG-3 to their generated
   artifacts, reproduction command, source fingerprint, environment, and
   limitations. The blocked historical VG-2 wording is not presented as proof.
5. In the operator workflow, inspect the decision contract, constraint-rejected
   state, and power inputs. Try launching without the review checkbox, then
   confirm the review and launch immutable version 1.
6. Confirm that Data health appears before Effect and uncertainty. Select
   **Inject missing exposure**. The effect is withheld and the decision action
   becomes unavailable.
7. Clear the fixture and record a decision. The log contains a frozen evidence
   payload rather than a link to mutable current state.

## Operator guarantees demonstrated

- Repeating a local assignment returns identical diagnostics, and changing the
  experiment version changes the deterministic hash namespace.
- Generated assignment, calibration, and integrity evidence is rendered only
  from approved manifests; unavailable or unapproved artifacts fail closed.
- Five adversarial event fixtures execute through the real local ingestion and
  reconciliation pipeline with expected invariants shown before results.
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

The workflow tests cover the three evidence sections, approved-manifest values,
repeat assignment and version isolation, five pipeline integrity scenarios,
valid and rejected states, missing inputs, empty state space, immutable launch
review, data-health ordering, seven injected integrity failures, chart labels,
evidence snapshots, and lifecycle controls.

## Rollback

The dashboard is isolated to `src/variantgrid/dashboard.py`,
`tests/test_dashboard.py`, and `dashboard/`. Removing those paths removes the
sandbox without changing the registry, assignment, ingestion, or statistical
modules.

## Honest limitations

- State is process-local and is lost when the server stops.
- There is no authentication, authorization, CSRF protection, multi-operator
  concurrency control, or durable audit store. A public deployment must remain
  a synthetic portfolio demo and must never receive private or production data.
- Health and metric values are explicit fixtures; this surface does not prove
  production ingestion or distributed failure recovery.
- Assignment scale and latency values are generated local benchmark evidence,
  not production traffic measurements or network-service latency.
- A/A calibration uses seeded synthetic simulations. Event Integrity Lab uses
  deterministic in-memory SQLite fixtures, not sustained Postgres load.
- The chart is an accessible HTML readout rather than a full visualization
  system.
- The dashboard demonstrates correct blocking behavior against existing core
  interfaces. Rendering a check does not independently prove the backend
  guarantee behind it.
