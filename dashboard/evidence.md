# Dashboard acceptance evidence

## Automated workflow artifact

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_dashboard -v
```

The test suite pairs each critical UI state with an assertion:

Latest local result: **13/13 dashboard workflow tests passed**. The runnable
server was also started on localhost and its root page returned the expected
HTML document. This is a functional smoke check, not a load or browser-
compatibility benchmark.

| Required state | Assertion |
|---|---|
| Assignment proof | Repeat diagnostics match; version namespace is isolated |
| Assignment benchmark | Approved VG-2R scale, drift, and latency render from generated artifacts |
| Event integrity proof | Five named scenarios execute through the local pipeline and pass |
| Validation evidence | Only approved VG-1, VG-2R, and VG-3 manifests provide public numbers |
| Claim limitations | Reproduction command and local/simulation limitations are visible |
| Valid creation | Default draft produces three valid states |
| Rejected state | Rejected combination retains its human-readable reason |
| Empty state space | Validation and launch both fail |
| Missing metric | Launch fails |
| Missing guardrail threshold | Launch fails |
| Launch review | Launch fails without explicit confirmation |
| Immutable running version | Direct edits fail; clone preserves original JSON |
| Running health | Health section precedes the readout |
| SRM | Effects withheld; decision raises |
| Missing exposure | Effects withheld; decision raises |
| Ingestion delay | Effects withheld; decision raises |
| Guardrail breach | Effects withheld; decision raises |
| Underpowered readout | Effects withheld; decision raises |
| Stale config/version disagreement | Effects withheld; decision raises |
| Decision record | Frozen configuration and readout are embedded in evidence |
| Pause, stop, kill | Valid transitions preserve configuration |
| Rollback and clone | Both version snapshots remain present and unchanged |
| Chart context | Denominator, unit, UTC range, and version are asserted |

## Manual render matrix

- Browser target: modern Chromium, Firefox, or Safari.
- Intended viewport: responsive from 375px wide; primary review at 1440×900.
- Test data: deterministic local onboarding fixture in `default_draft()` and
  `MetricFixture`; signed generated manifests; deterministic integrity fixtures.
- Database access: no external database. Event Integrity Lab uses process-local
  in-memory SQLite through the production event-store interface.
- Experiment version: version 1, plus clone/rollback exercise with version 2.
- Expected walkthrough duration: under two minutes; not yet instrumented as a
  benchmark claim.

## Evidence limitations

No screenshot is checked into this change. Automated HTML and workflow
assertions are the primary evidence artifact. A release owner should capture
Assignment Lab, Event Integrity Lab, Validation Evidence, the healthy launch,
and at least one injected-failure screen against the final integrated build so
screenshots do not become stale relative to the code.
