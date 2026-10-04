# VariantGrid case study: make growth decisions you can trust

## Problem

Running a product experiment is easy to imitate and hard to make trustworthy. A team can render two variants while silently changing assignments, counting assignments as exposure, double-counting retries, mixing versions, or declaring a winner after a data-quality failure. Those failures create speed without reliable learning.

## Engineering approach

VariantGrid models an experiment as an immutable versioned contract. Typed variables and constraints generate canonical product states. SHA-256 allocation maps a subject to a weighted state deterministically and returns the probability and bucket interval needed to debug that decision.

The measurement path keeps assignment, exposure, goals, observations, and guardrails distinct. Stable identifiers absorb retries, occurrence and receipt timestamps preserve late-delivery evidence, and event-time reconciliation admits only outcomes after a genuine same-version exposure and inside the declared attribution window.

The analysis layer reports binary and continuous uncertainty, power/MDE, SRM, guardrails, sequential-monitoring warnings, and multiplicity warnings. A failure is actionable: it removes the recommendation rather than decorating an otherwise confident result.

The Python SDK reduces the common host integration to user context, value retrieval, and outcome tracking. The operator sandbox makes constrained-state preview, launch review, data health, failure injection, decision snapshots, and rollback inspectable without direct database access.

## Evidence

One command recreates the project in a clean temporary copy, treats resource leaks as test failures, runs the complete suite and public benchmark, and emits claim manifests. Fixed seeds preserve the named 10,000-simulation calibration fixture while additional 1%, 10%, and 50% baselines test whether it generalizes. Assignment checks cover 50/50, 80/20, and 90/10 policies at one million subjects each. Event tests name duplicate, missing, late, reordered, and cross-version failure categories explicitly.

The exact current measurements and statuses are generated in `benchmarks/results.json` and `artifacts/release_audit.json`. Historical 1.46 microsecond latency language remains blocked when fresh repeated measurements do not support it.

The completed CodeArchitect study exercises that decision contract on real human
labels rather than a synthetic conversion fixture. VariantGrid replays two semantic
acceptance policies over 151 repository-disjoint components. The `0.62` policy
clears its 20% accepted-coverage bar at 23.2%, but strict human-reviewed precision
falls to 68.6%, breaching both the 80% floor and the maximum five-point drop from
the `0.72` policy. VariantGrid records **no-ship** with the frozen inputs and gate
outcomes attached. Because both policies run on the same components, this is an
offline paired model-policy evaluation, not randomized causal evidence.

## Outcome and boundary

The result is a credible local V1 and demonstration package: another engineer can define, launch, integrate, break, inspect, and reproduce an experiment decision, with the persistence contract exercised against PostgreSQL 17. It is not yet a production experimentation service. The remaining frontier is deployed security, durable asynchronous delivery, service observability, sustained load/chaos recovery, and real customer usage.
