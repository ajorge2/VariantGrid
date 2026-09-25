"""Runnable three-call VariantGrid integration.

Run with: PYTHONPATH=src python3 examples/reference_integration.py
"""

from variantgrid.api import VariantGridAPI
from variantgrid.events import EventStore
from variantgrid.models import Experiment, Factor
from variantgrid.registry import ExperimentRegistry, Lifecycle
from variantgrid.sdk import InProcessTransport, SDKConfig, VariantGridClient


experiment = Experiment.from_factors(
    key="onboarding_optimization",
    version=1,
    factors=(Factor("starter_type", ("template", "guided")),),
    primary_metric="activated",
    salt="reference-integration",
)
registry = ExperimentRegistry()
registry.create(experiment)
registry.transition(experiment.key, experiment.version, Lifecycle.RUNNING)
events = EventStore()
client = VariantGridClient(
    SDKConfig(experiment_versions={experiment.key: experiment.version}),
    InProcessTransport(VariantGridAPI(registry, events)),
)

# The host application's complete integration is three conceptual calls.
assignment = client.for_user("onboarding_optimization", "user-42")  # 1: context
starter_type = assignment.get("starter_type", default="template")  # 2: value + exposure
assignment.goal("activated", idempotency_key="activation:user-42:v1")  # 3: outcome

assert client.flush()
print({"starter_type": starter_type, "debug": assignment.debug_metadata.__dict__})
print({"stored_events": events.event_count()})
client.close()
events.close()
