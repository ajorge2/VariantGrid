# Inference integrity

VariantGrid's analysis layer treats a result as a fixed-horizon comparison unless the built-in Bonferroni sequential correction is explicitly declared. Binary readouts estimate the treatment-minus-control conversion-rate difference with an unpooled normal interval and relative lift with a log risk-ratio interval. Continuous readouts estimate the treatment-minus-control mean difference with a Welch standard error and use the delta method for relative-lift uncertainty. Every readout carries immutable method metadata describing the estimand, method version, effective per-look alpha, stopping rule, and assumptions. Unknown adjustment labels are rejected rather than accepted as proof that peeking was handled.

## Decision gates

A statistically significant effect is not sufficient for a recommendation. VariantGrid suppresses the recommendation when any required integrity gate fails:

- either state is below the predeclared minimum sample size;
- the observed allocation fails the sample-ratio-mismatch threshold;
- a declared guardrail regresses beyond its threshold;
- repeated analysis occurs without a declared sequential adjustment;
- the current analysis look exceeds the predeclared plan;
- the normal approximation has degenerate variance; or
- continuous outcomes show extreme-tail behavior incompatible with the default mean-based normal readout.

Unadjusted segment multiplicity is surfaced as a warning. The primary-metric recommendation is not suppressed solely because exploratory segment analyses exist, but segment findings must not be presented as confirmatory without a declared multiplicity procedure.

## Planning

Binary power and minimum detectable effect use the same two-sided normal approximation as the binary readout. Required sample size and MDE are numerically inverted from the power function so their contract is internally consistent. Continuous power and MDE require a predeclared standard deviation. These calculations establish behavior under their modeled assumptions; they do not prove real-world causal identification, absence of interference, or robustness to arbitrary outcome distributions.

## Heavy-tailed outcomes

The default continuous method flags extreme standardized observations. A flagged readout remains inspectable, but no winner is emitted. Production use should predeclare a suitable robust or transformed estimand rather than silently removing outliers after observing results.

## Reproducibility and limitations

`AnalysisPlan` and `StatisticalMethodMetadata` serialize to canonical JSON for storage alongside an immutable experiment version and decision snapshot. This module does not itself enforce storage durability or exposure/version correctness; it assumes the event and attribution layer supplies exposure-gated, version-clean inputs. The benchmark harness separately owns the frozen 10K A/A calibration matrix and its historical baseline fixture.
