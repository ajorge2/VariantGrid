from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from wsgiref.simple_server import make_server

from variantgrid.api import VariantGridAPI, VariantGridWSGIApp
from variantgrid.events import EventStore
from variantgrid.registry import (
    ExperimentRegistry,
    Lifecycle,
    configuration_sha256,
    serialize_experiment,
)


STUDY_DIR = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "demandmap_register_experiment",
    STUDY_DIR / "register_experiment.py",
)
assert SPEC and SPEC.loader
study = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(study)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DemandMap study service.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Move the preregistered draft to running before serving assignments.",
    )
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)

    experiment = study.build_experiment()
    registry = ExperimentRegistry(args.data_dir / "registry.sqlite3")
    try:
        try:
            registered = registry.get(experiment.key, experiment.version)
        except KeyError:
            registered = registry.create(experiment)

        expected_hash = configuration_sha256(serialize_experiment(experiment))
        if registered.configuration_sha256 != expected_hash:
            raise RuntimeError(
                "Stored study configuration differs from the preregistered v1 definition. "
                "Create a new immutable version instead of editing the launched study."
            )
        if args.launch and registered.lifecycle is Lifecycle.DRAFT:
            registered = registry.transition(
                experiment.key,
                experiment.version,
                Lifecycle.RUNNING,
            )
        if registered.lifecycle is not Lifecycle.RUNNING:
            raise RuntimeError(
                "Study is not running. Complete browser/event verification, then restart "
                "with --launch to begin enrollment."
            )

        events = EventStore(args.data_dir / "events.sqlite3")
        app = VariantGridWSGIApp(VariantGridAPI(registry, events))
        try:
            with make_server(args.host, args.port, app) as server:
                print(
                    f"DemandMap study API listening at http://{args.host}:{args.port} "
                    f"with data in {args.data_dir.resolve()}"
                )
                server.serve_forever()
        finally:
            events.close()
    finally:
        registry.close()


if __name__ == "__main__":
    main()
