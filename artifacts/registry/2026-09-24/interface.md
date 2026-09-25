# Registry interface evidence

The registry returns the existing `Experiment` object through
`RegisteredVersion.experiment`; assignment and API consumers therefore retain
their established contract.

Additive interfaces:

- `Experiment.preview_states()` exposes accepted and rejected configurations.
- `Experiment.replay_states()` proves regeneration against frozen identities.
- `ExperimentRegistry.replay(key, version)` checks byte-stable assignment input.
- `ExperimentRegistry.clone_with_edits(...)` creates the mandatory new draft
  for material changes.
- `ExperimentRegistry.metrics`, `guardrails`, and `decisions` expose persisted
  decision inputs and history.
- `ExperimentRegistry.persistence_snapshot` provides an auditable summary.

The full API/SDK and assignment fixture tests passed with these additive model
fields, including identical direct, service, and local-SDK assignment outputs.

