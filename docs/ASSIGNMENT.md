# Assignment contract

VariantGrid's fixed-weight evaluator maps a subject to exactly one state from an immutable experiment version. The local SDK and service both call the same `AssignmentEngine`; `tests/fixtures/assignment_parity.json` freezes representative outputs so a change to the hash contract cannot silently move users.

## SHA-256 v1

`sha256-v1` hashes this UTF-8 token and converts the first eight digest bytes into a unit interval bucket:

```text
{experiment_key}:{experiment_version}:{salt}:{algorithm_version}:{policy_version}:{subject_id}
```

The evaluator selects the state whose cumulative normalized weight contains the bucket. Experiment key, version, salt, algorithm version, and policy version are all assignment namespaces. Changing any of them intentionally creates a new assignment population. The exact token format is frozen for `sha256-v1`; any future hash format must use a new algorithm version.

State weights must be finite and positive. One-state and many-state experiments use the same path. Unsupported algorithms and policy families fail before an exposure can be emitted.

## Eligibility and safe fallback

The experiment rules `all` and `none` are resolved locally. Any richer rule must be evaluated by a trusted caller against subject attributes and supplied as an explicit boolean. If context for a richer rule is absent, assignment fails closed with `eligibility_context_required`. Ineligible responses have no state, zero assignment probability, and immutable fallback values supplied by the caller.

Service outages are handled one layer above the engine by the SDK: a fresh cached assignment may be used; otherwise the product receives its call-site default. Both paths are visible in SDK debug metadata.

## Diagnostics

Each engine result records the experiment and policy versions, selected state ID, assignment probability, eligibility decision, and reason. Local engine diagnostics additionally expose the unit bucket, selected cumulative interval, total weight, selected weight, eligibility rule, and whether eligibility came from the experiment rule or the caller. Raw digests and salts are not repeated in diagnostics.

## Verification and rollback

Run assignment correctness and parity tests with:

```text
PYTHONPATH=src python3 -m unittest tests.test_assignment -v
```

Then run the full suite. Roll back assignment behavior by serving a previously registered immutable experiment version. Do not edit a launched version or change the implementation behind `sha256-v1`.

The unit tests prove deterministic local behavior and local/service SDK parity against in-process fixtures. They do not prove network latency, distributed availability, production throughput, or correct evaluation of arbitrary customer eligibility expressions. Allocation and latency claims require the separately owned benchmark artifacts and disclosed methodology.
