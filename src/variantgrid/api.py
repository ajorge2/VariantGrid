from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from threading import Lock
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs

from .assignment import AssignmentEngine, AssignmentResult
from .events import Event, EventStore
from .registry import ExperimentRegistry, Lifecycle


API_SCHEMA_VERSION = "1"
SDK_COMPATIBILITY_HEADER = "X-VariantGrid-Schema-Version"


@dataclass(frozen=True)
class APIRequest:
    method: str
    path: str
    body: Mapping[str, Any] | None = None
    headers: Mapping[str, str] | None = None


@dataclass(frozen=True)
class APIResponse:
    status: int
    body: Mapping[str, Any]


def assignment_payload(result: AssignmentResult) -> dict[str, Any]:
    """Return the canonical wire representation for an assignment."""
    diagnostics = asdict(result.diagnostics) if result.diagnostics is not None else None
    return {
        "experiment_key": result.experiment_key,
        "experiment_version": result.experiment_version,
        "state_id": result.state_id,
        "state_key": result.state.key if result.state is not None else None,
        "values": dict(result.state.values) if result.state is not None else {},
        "assignment_probability": result.assignment_probability,
        "algorithm_version": result.algorithm_version,
        "policy_version": result.policy_version,
        "eligible": result.eligible,
        "fallback_values": dict(result.fallback_values) if result.fallback_values is not None else None,
        "reason": result.reason,
        "diagnostics": diagnostics,
    }


class VariantGridAPI:
    """Dependency-light implementation of VariantGrid's versioned HTTP contract.

    ``handle`` is transport independent and is used by both the WSGI adapter and
    in-process compatibility tests. Assignment remains delegated to the shared
    ``AssignmentEngine``; event idempotency remains delegated to ``EventStore``.
    """

    def __init__(self, registry: ExperimentRegistry, event_store: EventStore) -> None:
        self.registry = registry
        self.event_store = event_store
        self._known_versions: set[tuple[str, int]] = set()
        self._known_versions_lock = Lock()

    def handle(self, request: APIRequest) -> APIResponse:
        headers = {key.lower(): value for key, value in (request.headers or {}).items()}
        requested_schema = headers.get(SDK_COMPATIBILITY_HEADER.lower(), API_SCHEMA_VERSION)
        if requested_schema != API_SCHEMA_VERSION:
            return self._error(
                426,
                "incompatible_schema",
                f"API schema {API_SCHEMA_VERSION} does not support client schema {requested_schema}",
            )

        method = request.method.upper()
        path = request.path.rstrip("/") or "/"
        try:
            if method == "GET" and path == "/v1/health":
                return APIResponse(200, {"ok": True, "schema_version": API_SCHEMA_VERSION})
            if method == "POST" and path == "/v1/assign":
                return self._assign(request.body or {})
            if method == "POST" and path == "/v1/events/batch":
                return self._ingest(request.body or {})
        except (KeyError, TypeError, ValueError) as error:
            return self._error(400, "invalid_request", str(error))
        return self._error(404, "not_found", f"No route for {method} {path}")

    def _assign(self, body: Mapping[str, Any]) -> APIResponse:
        experiment_key = str(body["experiment_key"])
        experiment_version = int(body["experiment_version"])
        subject_id = str(body["subject_id"])
        registered = self.registry.get(experiment_key, experiment_version)
        if registered.lifecycle is not Lifecycle.RUNNING:
            return self._error(409, "experiment_not_running", registered.lifecycle.value)
        defaults = body.get("fallback_values")
        if defaults is not None and not isinstance(defaults, Mapping):
            raise ValueError("fallback_values must be an object")
        explicit_eligibility = bool(body["eligible"]) if "eligible" in body else None
        result = AssignmentEngine(registered.experiment).assign_with_metadata(
            subject_id,
            eligible=explicit_eligibility,
            fallback_values=defaults,
        )
        if result.state is not None:
            subject_digest = sha256(subject_id.encode("utf-8")).hexdigest()[:20]
            self.event_store.ingest(
                Event(
                    event_id=(
                        f"assignment:{experiment_key}:{experiment_version}:"
                        f"{subject_digest}:{result.state_id}"
                    ),
                    experiment_key=experiment_key,
                    experiment_version=experiment_version,
                    subject_id=subject_id,
                    event_type="assignment",
                    variant_key=result.state.key,
                )
            )
        # SDK tracking is asynchronous. Remember versions validated on the
        # assignment thread so a SQLite-backed local registry is not reopened
        # from the delivery worker thread. Production registries may validate
        # directly from any request worker.
        with self._known_versions_lock:
            self._known_versions.add((experiment_key, experiment_version))
        return APIResponse(
            200,
            {"schema_version": API_SCHEMA_VERSION, "assignment": assignment_payload(result)},
        )

    def _ingest(self, body: Mapping[str, Any]) -> APIResponse:
        raw_events = body.get("events")
        if not isinstance(raw_events, list) or not raw_events:
            raise ValueError("events must be a non-empty array")
        events: list[Event] = []
        for raw in raw_events:
            if not isinstance(raw, Mapping):
                raise ValueError("each event must be an object")
            event = Event(**dict(raw))
            # Reject unknown versions at the service boundary rather than
            # allowing events to create an unverifiable experiment history.
            version_identity = (event.experiment_key, event.experiment_version)
            with self._known_versions_lock:
                known = version_identity in self._known_versions
            if not known:
                self.registry.get(*version_identity)
                with self._known_versions_lock:
                    self._known_versions.add(version_identity)
            event.validate()
            events.append(event)
        inserted, duplicates = self.event_store.ingest_many(events)
        return APIResponse(
            202,
            {
                "schema_version": API_SCHEMA_VERSION,
                "accepted": inserted,
                "duplicates": duplicates,
                "event_ids": [event.event_id for event in events],
            },
        )

    @staticmethod
    def _error(status: int, code: str, message: str) -> APIResponse:
        return APIResponse(
            status,
            {
                "schema_version": API_SCHEMA_VERSION,
                "error": {"code": code, "message": message},
            },
        )


class VariantGridWSGIApp:
    """Small WSGI adapter for the stable API contract.

    Production deployment still requires authentication, request-size limits,
    structured logging, and a hardened WSGI server.
    """

    def __init__(self, api: VariantGridAPI) -> None:
        self.api = api

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw_body = environ["wsgi.input"].read(length) if length else b""
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            response = VariantGridAPI._error(400, "invalid_json", str(error))
        else:
            headers = {
                key[5:].replace("_", "-"): str(value)
                for key, value in environ.items()
                if key.startswith("HTTP_")
            }
            response = self.api.handle(
                APIRequest(
                    method=str(environ.get("REQUEST_METHOD", "GET")),
                    path=str(environ.get("PATH_INFO", "/")),
                    body=body,
                    headers=headers,
                )
            )
        payload = json.dumps(response.body, separators=(",", ":")).encode("utf-8")
        status_text = {
            200: "200 OK",
            202: "202 Accepted",
            400: "400 Bad Request",
            404: "404 Not Found",
            409: "409 Conflict",
            426: "426 Upgrade Required",
        }.get(response.status, f"{response.status} Error")
        start_response(
            status_text,
            [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))],
        )
        return [payload]
