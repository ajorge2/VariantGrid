# DemandMap guided-calibration experiment v1

Status: implementation preregistration; do not start enrollment until the event
parity test and browser smoke test pass.

## Decision

Decide whether DemandMap should replace its current self-serve calibration screen
with a guided workflow that recommends an initial persona count, asks the user to
inspect representative-account evidence, and then advances to market research.

## Hypothesis

The guided workflow will increase the share of exposed participants who complete
a market-research result within 10 minutes, without reducing successful research
requests by more than 5 percentage points.

## Population and assignment

- Unit: one recruited participant, identified by a stable study ID.
- Allocation: 50/50 deterministic assignment through VariantGrid.
- Control: the current self-serve calibration interface.
- Treatment: a three-step guided calibration interface with the same underlying
  clustering models and data. It recommends the diagnostic-selected initial K,
  prompts the participant to inspect at least one representative-account panel,
  and presents one explicit continue-to-research action.
- Repeat visits retain the original assignment.
- Staff, automated checks, and instrumentation smoke tests use reserved IDs and
  are excluded before enrollment begins.

## Outcomes

### Primary metric

`research_completed`: binary. A participant receives a successful, rendered
market-research result within 10 minutes of first exposure.

### Guardrail

`research_request_success`: binary. A submitted market-research request returns a
valid response. The treatment may not reduce this rate by more than 5 percentage
points.

### Secondary metrics

- `evidence_reviewed_before_research`: binary; the participant opens at least one
  supporting-customer or representative-account panel before submitting.
- `time_to_research_seconds`: continuous elapsed time from first exposure to the
  first successful result.
- `selected_persona_count`: continuous number of personas included in the first
  submitted request.

Secondary metrics are descriptive and cannot independently trigger a ship call.

## Sample size and analysis

- Assumed control completion rate: 40%.
- Minimum detectable absolute increase: 25 percentage points.
- Alpha: 0.05, two-sided.
- Target power: 80%.
- Required sample: 59 exposed participants per variant, 118 total.
- Stopping rule: one fixed-horizon analysis after both variants reach 59 exposed
  participants. No unadjusted interim significance checks.
- Sample-ratio-mismatch threshold: p < 0.001.
- Estimand: treatment completion rate minus control completion rate.
- Primary method: unpooled two-proportion normal test and 95% interval, matching
  VariantGrid's `two-proportion-z-v1` contract.

The 40% baseline is a planning assumption, not an observed result. If early
instrumentation reveals it is materially wrong, create and launch a new immutable
experiment version before confirmatory enrollment; do not resize v1 after looking
at treatment effects.

## Decision rule

- **Ship guided calibration:** minimum sample is met, no SRM or exposure-integrity
  failure exists, the guardrail passes, and the primary 95% confidence interval is
  entirely above zero.
- **Keep self-serve:** the primary interval is entirely below zero or the guardrail
  fails for a treatment-caused reason.
- **No decision:** the interval crosses zero and the guardrail passes. Preserve the
  result; do not relabel it as evidence of equivalence.

## Integrity requirements

- Assignment is not exposure. Exposure is logged only after the assigned interface
  renders successfully.
- Outcome events must use the same experiment version as exposure.
- One stable event ID is reused for retries.
- Receipt order does not replace event time.
- Participants with no exposure cannot enter the primary denominator.
- A failed or timed-out research request cannot count as `research_completed`.
- The event schema and direct/SDK assignment must pass parity tests before launch.

## Resume claim unlocked only after completion

If the study reaches its fixed horizon and produces a decision, report the sample,
absolute effect and interval, guardrail result, and ship/keep decision. Do not call
the interface better from pilot data, implementation completion, or a positive
point estimate whose interval crosses zero.

## Local service

After the DemandMap browser integration and event-parity tests pass, start the
study service with an explicit launch action:

```sh
PYTHONPATH=src python3 studies/demandmap-guided-calibration/run_service.py \
  --data-dir /absolute/path/to/study-data \
  --launch
```

The service refuses to assign participants while the preregistered version is a
draft and refuses to start if the stored configuration differs from the versioned
study definition.
