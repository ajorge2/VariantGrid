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

## Proposed extensible inference framework

**Status: design note; not yet implemented.** VariantGrid should grow from its current built-in frequentist readouts into a template-first inference framework with an extension interface for custom methods. The platform continues to own experiment configuration, exposure-valid data, integrity checks, provenance, guardrails, and decision recording. The inference layer estimates effects and uncertainty; the decision layer determines whether that evidence is sufficient to act.

### Shared workflow

Both frequentist and Bayesian methods should follow the same outer workflow:

1. Define the population, treatments, metric, and estimand.
2. Validate grouping, exposure, outcomes, and required sample structure.
3. Run the configured inference method.
4. Quantify uncertainty and emit assumption diagnostics.
5. Apply a separately configured decision policy.
6. Store the method version, inputs, results, diagnostics, and decision as an auditable record.

### Reusable and composed workflow definitions

An experiment workflow or any validated subgraph may be saved as a reusable definition and referenced from another workflow like a typed function. A reusable definition declares an input schema, output schema, configuration parameters, validation contract, and immutable version. A parent workflow binds its inputs and parameters, receives its declared outputs, and preserves the complete expanded lineage in the final evidence record.

This enables reusable components for eligibility, cleaning, metric construction, assignment, inference, and decision gates. It also supports factorial software experiments: an assignment component can construct the valid combinations of treatment factors, remove combinations that violate declared constraints, and deterministically randomize eligible subjects across the remaining canonical states using configured weights, experiment/version identity, and salt.

Treatment combinations and telemetry events remain distinct. Randomization selects combinations of product parameters or variant states; assignment, exposure, goal, observation, and guardrail events record what subsequently happened. Calling a reusable component must not silently introduce another randomization layer, change inclusion probabilities, or reinterpret an event as a treatment.

Composition must therefore enforce these rules:

- every reference pins an immutable component version;
- inputs and outputs are typed and validated at the call boundary;
- parameter bindings and defaults are included in provenance;
- recursive calls and cyclic workflow graphs are rejected;
- node and metric namespaces cannot collide after expansion;
- a subject has one authoritative assignment path for a given experiment version;
- composed randomization declares its unit, weights, constraints, and independence assumptions; and
- the stored result can be expanded back into the exact component graph that executed.

### Validated templates

Most users should choose a validated analysis template rather than assemble low-level statistical settings. Initial templates could include:

- binary conversion comparison;
- continuous mean comparison;
- paired policy evaluation, such as evaluating two model thresholds on the same held-out units;
- multi-arm experiment with declared multiplicity handling;
- non-inferiority test with a predeclared acceptable-loss margin; and
- Bayesian binary experiment.

A template constrains the compatible metric type, estimand, grouping structure, inference method, assumptions, and decision settings.

### Frequentist specification

A frequentist method declares its null value, test statistic, reference-distribution method, alternative, significance level, confidence level, grouping assumptions, and any sequential or multiplicity correction. Standard output includes the effect estimate, standard error, statistic, p-value, confidence interval, assumptions, and warnings.

### Bayesian specification

A Bayesian method declares its likelihood, prior, estimand, posterior summaries, credible-interval level, and optional practical-equivalence region, minimum useful effect, Bayes-factor comparison, or expected-loss rule. Standard output includes the posterior effect distribution or its summaries, credible interval, relevant posterior probabilities, assumptions, and warnings. Bayesian inference is not required to frame every analysis as a null-versus-alternative test.

### Custom methods

Advanced users may register a custom frequentist test, resampling procedure, causal estimator with inferential outputs, or Bayesian model. Every custom method must declare:

- supported metric and data types;
- estimand and comparison structure;
- required assumptions and minimum sample structure;
- frequentist null/reference procedure or Bayesian likelihood/prior;
- uncertainty calculation;
- validation and failure conditions;
- output interpretation; and
- immutable method name and version.

Custom methods must return a shared `InferenceResult` contract containing, where applicable, an estimate, interval, evidence fields such as a p-value or posterior probability, diagnostics, assumptions, warnings, and method provenance. VariantGrid should reject incoherent combinations rather than accept arbitrary settings as proof of valid inference.

### Input and method validation

Every validated template and custom inference method must pass the same fail-closed validation boundary before execution. A malformed or statistically incoherent specification must produce an inspectable error and must never emit a decision-eligible result.

Validation occurs at five layers:

1. **Schema validation** checks required fields, types, enumerated values, finite numbers, valid probability ranges, and immutable method/version identifiers.
2. **Compatibility validation** rejects incoherent combinations, such as a binary-outcome test applied to continuous data, a paired method without stable pair identifiers, a one-sided decision with an incompatible alternative, or a sequential correction without a declared look plan.
3. **Data-precondition validation** checks required groups, sample sizes, exposure eligibility, missingness rules, variation, independence or pairing structure, supported weights, and any method-specific assumptions that can be checked mechanically.
4. **Numerical validation** rejects non-finite intermediate values, degenerate variance, singular calculations, invalid posterior normalization, non-convergence, or outputs outside their mathematical domains.
5. **Result validation** checks that the returned `InferenceResult` is complete, internally consistent, correctly versioned, and compatible with the configured decision policy. For example, p-values and posterior probabilities must lie in `[0, 1]`, interval bounds must be ordered, and required diagnostics must be present.

Templates should ship with fixture datasets covering ordinary, boundary, invalid, and adversarial cases. Custom methods must provide their own validator and conformance fixtures against the shared contract. Registration should include a dry run, and any later change to code, defaults, assumptions, or validation rules must create a new immutable method version. Warnings may preserve an inspectable result, but failed requirements block inference and decision recording rather than silently falling back to another method.

### Decision boundary

Inference and action remain separate. A frequentist decision policy might require a corrected p-value below its threshold and a confidence interval beyond the minimum useful effect. A Bayesian policy might require a specified posterior probability that the effect exceeds a practical threshold and an acceptable expected loss. Both still pass through VariantGrid's sample-ratio, exposure, event-integrity, and business-guardrail gates before a ship recommendation can be recorded.
