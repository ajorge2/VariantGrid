"""Run the dependency-light local HTTP service on 127.0.0.1:8080."""

from wsgiref.simple_server import make_server

from variantgrid.api import VariantGridAPI, VariantGridWSGIApp
from variantgrid.events import EventStore
from variantgrid.models import Experiment, Factor
from variantgrid.registry import ExperimentRegistry, Lifecycle


experiment = Experiment.from_factors(
    key="onboarding_optimization",
    version=1,
    factors=(Factor("starter_type", ("template", "guided")),),
    primary_metric="activated",
    salt="local-service",
)
registry = ExperimentRegistry()
registry.create(experiment)
registry.transition(experiment.key, experiment.version, Lifecycle.RUNNING)
app = VariantGridWSGIApp(VariantGridAPI(registry, EventStore("variantgrid-events.sqlite3")))

with make_server("127.0.0.1", 8080, app) as server:
    print("VariantGrid API listening at http://127.0.0.1:8080")
    server.serve_forever()
