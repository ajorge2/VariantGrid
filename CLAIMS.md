# Protected Claim Registry

Status values: `historical`, `reproduced`, `blocked`, or `approved`.

| ID | Claim | Primary owner | Independent signer | Current status |
|---|---|---|---|---|
| VG-1 | Typed multivariate variants, constraint-aware states, deterministic versioned assignment, and idempotent exposure logging produce trustworthy local readouts. | Registry engineer, with assignment and ingestion sign-off | Evidence/release engineer | approved |
| VG-2 | 100% repeat-user stickiness across 1M assignments, 0.06 percentage-point allocation drift, and 1.46 microsecond sampled p95 local assignment latency. | Assignment engineer | Evidence/release engineer | blocked |
| VG-2R | 100% repeat-user stickiness across three 1M-assignment policies, at most 0.0402 percentage-point drift, and 1.417 microsecond median sampled p95 local latency across five trials. | Assignment engineer | Evidence/release engineer | approved |
| VG-3 | 5.13% false-positive rate and 94.87% interval coverage across 10K A/A simulations, plus five adversarial integrity categories. | Inference integrity engineer, with ingestion sign-off | Evidence/release engineer | approved |

## Baseline note

The copied historical artifacts report all three numbers in VG-2. The clean-copy release run reproduced 100% stickiness and at most 0.0402 percentage-point drift across three allocation policies, and measured a 1.417-microsecond median sampled p95 with a 1.416–1.958 range. The exact historical wording remains blocked because the generated evidence now supports a different measured value; use VG-2R or say "under 1.5 microseconds."

VG-1, VG-2R, and VG-3 were independently regenerated from a clean checkout-like staged copy. Their approval applies to the local implementation and stated simulation/test scope, not production durability, adoption, uptime, revenue, or causal business lift.
