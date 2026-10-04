# CodeArchitect threshold evaluation through VariantGrid

This completed study replays CodeArchitect's frozen human-review evidence through
VariantGrid's pre-registered policy-gate and immutable decision-record path.

It is an **offline paired model-policy evaluation**, not a randomized A/B test:
both the `0.72` and `0.62` acceptance policies are evaluated on the same 151
repository-disjoint components. VariantGrid registers the two policy states,
validates the completed review evidence, recomputes coverage and precision from
the blinded human labels, applies the frozen gates, and stores the resulting
no-ship decision with source hashes in its SQLite registry.

Run from the VariantGrid repository:

```bash
PYTHONPATH=src python3 studies/codearchitect-threshold-evaluation/run_evaluation.py --replace
```

Generated evidence:

- `decision-snapshot.json` — readable, machine-verifiable result plus the full
  VariantGrid registry snapshot;
- `variantgrid.sqlite` — the launched policy version and immutable decision
  record.

The default evidence source is the sibling
`../codearchitect-decision-evidence/` directory. The generated snapshot records
SHA-256 digests for the human reviews, blinded review key, source manifest, and
frozen preregistration.

Expected decision: **no-ship** lowering CodeArchitect's semantic acceptance
threshold from `0.72` to `0.62`. Coverage clears its 20% bar, but strict precision
fails both the 80% absolute floor and the maximum five-point regression rule.
