# Two-minute VariantGrid demo

## Before the clock

From the repository root, start the local sandbox:

```bash
PYTHONPATH=src python3 -m variantgrid.dashboard --port 8766
```

Open `http://127.0.0.1:8766`.

## 0:00–0:30 — Prove deterministic assignment

In **Assignment Lab**, change the subject ID and select **Assign twice and compare versions**. Point out the identical repeated result, SHA-256 bucket and interval, experiment and policy versions, and the separate comparison-version namespace. A version can legitimately land in the same state; isolation means the version changed the deterministic hash namespace.

Then scan the generated 50/50, 80/20, and 90/10 rows: each contains one million assignments, 100% repeat-user stickiness, observed drift, and the five-trial local latency summary. These values are loaded from approved machine-readable artifacts; opening the page does not rerun the benchmark.

## 0:30–0:55 — Run the five integrity scenarios

In **Event Integrity Lab**, show that duplicate, missing-exposure, late, reordered, and cross-version fixtures all pass through the real local ingestion and reconciliation pipeline. Expand one observed result. Emphasize that assignment is not treated as exposure, retries do not double-count, event time controls late reconciliation, delivery order does not change the digest, and versions never cross-attribute.

What this proves locally: the named adversarial semantics hold for deterministic in-memory SQLite fixtures. It does not prove distributed delivery, Postgres throughput, or availability.

## 0:55–1:15 — Trace every published number

In **Validation Evidence**, connect the three resume bullets to approved VG-1, VG-2R, and VG-3 manifests. Expand provenance to show the reproduction command, source fingerprint, environment, artifacts, methodology, and limitations. Note that the historical VG-2 wording is blocked and therefore not displayed as evidence.

## 1:15–1:40 — Configure and freeze the experiment contract

In **Interactive operator workflow**, point out the hypothesis, primary metric, guardrail, eligibility, allocation, stopping rule, typed variables, rejected state, and power inputs. Try launching without approving the immutable review; it is blocked. Confirm the review and launch version 1.

## 1:40–2:00 — Make failure behavior visible

Show that **Data health** appears before **Effect and uncertainty**. Inject **missing exposure**: the effect disappears and the decision action is blocked. Clear it, record a decision, and show the immutable evidence snapshot. If time permits, clone the launched version to demonstrate change without rewriting history.

For clean-state reproduction outside the demo, run:

```bash
PYTHONPATH=src python3 scripts/verify_release.py
```

The release gate regenerates the manifests and confirms that replacement VG-2R wording comes from current evidence rather than copied history.

## Honest closing line

“This demo proves deterministic local assignment, calibrated seeded simulation, adversarial SQLite event semantics, and an auditable operator workflow. It does not claim distributed durability, production uptime, customer adoption, causal business impact, or revenue lift.”
