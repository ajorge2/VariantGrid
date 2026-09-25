# Two-minute VariantGrid demo

## Before the clock

From the repository root, start the local sandbox:

```bash
PYTHONPATH=src python3 -m variantgrid.dashboard --port 8766
```

Open `http://127.0.0.1:8766`.

## 0:00–0:30 — Turn a hypothesis into valid states

Point out the predeclared hypothesis, primary metric, guardrail, eligibility rule, allocation, and stopping rule. Preview the typed variables and constraint. The table preserves both valid states and the rejected `blank + guided tour` state with its rejection reason.

What this proves locally: configuration is explicit, invalid combinations are inspectable, and canonical states are generated before launch.

## 0:30–0:55 — Freeze the experiment contract

Try to launch without approving the review; launch is blocked. Confirm the review and launch version 1. The version freezes its configuration instead of allowing a live edit.

What this proves locally: required decision inputs and explicit review gate launch, and material changes require a new version.

## 0:55–1:25 — Make a trustworthy readout visible

Show that **Data health** appears before **Effect and uncertainty**. The healthy fixture exposes version, denominator, unit, time range, absolute effect, interval, p-value, and recommendation.

Then inject **missing exposure**. The effect disappears and the decision action is blocked. Repeat quickly with SRM or a guardrail breach if time permits.

What this proves locally: integrity failures are not decorative warnings; they suppress interpretation and action.

## 1:25–1:50 — Preserve the decision

Clear the failure, record a decision, and inspect the decision log. The record contains an immutable snapshot of the configuration, health state, readout, and recommendation rather than a link to mutable current state.

Pause and resume, then clone version 1 and roll back to demonstrate history without mutation.

## 1:50–2:00 — Show the proof, including the failure

Open `artifacts/release_audit.json` or run:

```bash
PYTHONPATH=src python3 scripts/verify_release.py
```

Show that the local release gate is generated from a clean-copy run, VG-1 and VG-3 are approved, the exact historical VG-2 latency wording is blocked, and replacement wording comes from the current benchmark rather than copied history.

## Honest closing line

“This demo proves deterministic local behavior, calibrated simulation, adversarial event semantics, and an auditable operator workflow. It does not claim deployed Postgres durability, production uptime, adoption, or business lift.”
