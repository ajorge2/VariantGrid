from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from queue import Empty, Full, Queue
from threading import Condition, Lock, Thread
from time import monotonic, sleep
from typing import Any, Callable, Generic, Mapping, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from .api import (
    API_SCHEMA_VERSION,
    SDK_COMPATIBILITY_HEADER,
    APIRequest,
    APIResponse,
    VariantGridAPI,
    assignment_payload,
)
from .assignment import AssignmentEngine
from .events import Event
from .models import Experiment


SDK_VERSION = "0.1.0"
T = TypeVar("T")


class TransportError(RuntimeError):
    pass


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> APIResponse: ...


class InProcessTransport:
    """Transport for tests, sandbox integrations, and service parity fixtures."""

    def __init__(self, api: VariantGridAPI) -> None:
        self.api = api

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> APIResponse:
        del timeout_seconds
        return self.api.handle(APIRequest(method, path, body, headers))


class UrllibTransport:
    """Standard-library HTTP transport with a mandatory finite timeout."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        timeout_seconds: float,
        headers: Mapping[str, str],
    ) -> APIResponse:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            self.base_url + path,
            data=payload,
            method=method,
            headers={"Content-Type": "application/json", **dict(headers)},
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
                return APIResponse(response.status, json.loads(raw))
        except HTTPError as error:
            try:
                error_body = json.loads(error.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                error_body = {"error": {"code": "http_error", "message": str(error)}}
            return APIResponse(error.code, error_body)
        except (OSError, TimeoutError, URLError) as error:
            raise TransportError(str(error)) from error


@dataclass(frozen=True)
class SDKConfig:
    experiment_versions: Mapping[str, int]
    request_timeout_seconds: float = 0.25
    cache_ttl_seconds: float = 60.0
    allow_stale_on_error: bool = True
    tracking_queue_size: int = 1_000
    tracking_max_attempts: int = 3
    tracking_retry_backoff_seconds: float = 0.01
    schema_version: str = API_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.experiment_versions:
            raise ValueError("experiment_versions must not be empty")
        if self.request_timeout_seconds <= 0 or self.request_timeout_seconds > 30:
            raise ValueError("request_timeout_seconds must be within (0, 30]")
        if self.cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must be non-negative")
        if self.tracking_queue_size < 1 or self.tracking_max_attempts < 1:
            raise ValueError("tracking queue size and attempts must be positive")
        if self.tracking_retry_backoff_seconds < 0:
            raise ValueError("retry backoff must be non-negative")


@dataclass(frozen=True)
class AssignmentDebug:
    experiment_key: str
    experiment_version: int
    state_id: str | None
    state_key: str | None
    algorithm_version: str | None
    policy_version: str | None
    assignment_probability: float
    eligible: bool
    source: str
    fallback_status: str | None
    from_cache: bool
    cache_age_seconds: float | None
    stale: bool
    bucket: float | None = None
    interval_start: float | None = None
    interval_end: float | None = None
    total_weight: float | None = None
    selected_weight: float | None = None
    eligibility_rule: str | None = None
    eligibility_source: str | None = None
    sdk_version: str = SDK_VERSION
    api_schema_version: str = API_SCHEMA_VERSION


@dataclass(frozen=True)
class AssignmentSnapshot:
    values: Mapping[str, Any]
    debug: AssignmentDebug


@dataclass(frozen=True)
class TrackingReceipt:
    event_id: str
    queued: bool
    reason: str | None = None


@dataclass(frozen=True)
class DeliveryFailure:
    event_id: str
    attempts: int
    reason: str


@dataclass(frozen=True)
class _CacheEntry:
    assignment: AssignmentSnapshot
    stored_at: float


class AsyncEventTracker:
    """Bounded, non-blocking event delivery with stable retry identities."""

    def __init__(self, transport: Transport, config: SDKConfig) -> None:
        self._transport = transport
        self._config = config
        self._queue: Queue[Event | None] = Queue(maxsize=config.tracking_queue_size)
        self._pending = 0
        self._condition = Condition()
        self._failures: list[DeliveryFailure] = []
        self._thread = Thread(target=self._run, name="variantgrid-events", daemon=True)
        self._thread.start()

    @property
    def failures(self) -> tuple[DeliveryFailure, ...]:
        with self._condition:
            return tuple(self._failures)

    def enqueue(self, event: Event) -> TrackingReceipt:
        with self._condition:
            self._pending += 1
        try:
            self._queue.put_nowait(event)
        except Full:
            with self._condition:
                self._pending -= 1
                self._condition.notify_all()
            return TrackingReceipt(event.event_id, False, "tracking_queue_full")
        return TrackingReceipt(event.event_id, True)

    def flush(self, timeout_seconds: float = 2.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("flush timeout must be non-negative")
        deadline = monotonic() + timeout_seconds
        with self._condition:
            while self._pending:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, timeout_seconds: float = 2.0) -> bool:
        flushed = self.flush(timeout_seconds)
        try:
            self._queue.put_nowait(None)
        except Full:
            return False
        self._thread.join(timeout=max(0.0, timeout_seconds))
        return flushed and not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=0.1)
            except Empty:
                continue
            if event is None:
                self._queue.task_done()
                return
            failure: str | None = None
            attempts = 0
            for attempts in range(1, self._config.tracking_max_attempts + 1):
                try:
                    response = self._transport.request(
                        "POST",
                        "/v1/events/batch",
                        {"events": [event.__dict__]},
                        timeout_seconds=self._config.request_timeout_seconds,
                        headers={SDK_COMPATIBILITY_HEADER: self._config.schema_version},
                    )
                    if response.status in (200, 202):
                        failure = None
                        break
                    failure = f"http_status_{response.status}"
                except Exception as error:  # delivery failures must not terminate the host thread
                    failure = f"{type(error).__name__}: {error}"
                if attempts < self._config.tracking_max_attempts:
                    sleep(self._config.tracking_retry_backoff_seconds * attempts)
            with self._condition:
                if failure is not None:
                    self._failures.append(DeliveryFailure(event.event_id, attempts, failure))
                self._pending -= 1
                self._condition.notify_all()
            self._queue.task_done()


class VariantGridClient:
    def __init__(
        self,
        config: SDKConfig,
        transport: Transport,
        *,
        local_experiments: Mapping[tuple[str, int], Experiment] | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.config = config
        self.transport = transport
        self.local_experiments = dict(local_experiments or {})
        self._clock = clock
        self._cache: dict[tuple[str, int, str, bool], _CacheEntry] = {}
        self._cache_lock = Lock()
        self._tracker = AsyncEventTracker(transport, config)

    @property
    def delivery_failures(self) -> tuple[DeliveryFailure, ...]:
        return self._tracker.failures

    def for_user(
        self,
        experiment_key: str,
        subject_id: str,
        *,
        eligible: bool | None = None,
        context_id: str | None = None,
    ) -> "ExperimentContext":
        if not subject_id:
            raise ValueError("subject_id is required")
        try:
            version = self.config.experiment_versions[experiment_key]
        except KeyError as error:
            raise KeyError(f"No configured version for {experiment_key!r}") from error
        assignment = self._resolve_assignment(experiment_key, version, subject_id, eligible)
        return ExperimentContext(
            client=self,
            experiment_key=experiment_key,
            experiment_version=version,
            subject_id=subject_id,
            context_id=context_id or uuid4().hex,
            assignment=assignment,
        )

    def flush(self, timeout_seconds: float = 2.0) -> bool:
        return self._tracker.flush(timeout_seconds)

    def close(self, timeout_seconds: float = 2.0) -> bool:
        return self._tracker.close(timeout_seconds)

    def _resolve_assignment(
        self, experiment_key: str, version: int, subject_id: str, eligible: bool | None
    ) -> AssignmentSnapshot:
        cache_key = (experiment_key, version, subject_id, eligible)
        now = self._clock()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            age = max(0.0, now - cached.stored_at)
            if age <= self.config.cache_ttl_seconds:
                return replace(
                    cached.assignment,
                    debug=replace(
                        cached.assignment.debug,
                        source="cache",
                        from_cache=True,
                        cache_age_seconds=age,
                        stale=False,
                    ),
                )

        try:
            if (experiment_key, version) in self.local_experiments:
                result = AssignmentEngine(self.local_experiments[(experiment_key, version)]).assign_with_metadata(
                    subject_id, eligible=eligible
                )
                snapshot = self._snapshot(assignment_payload(result), source="local")
            else:
                request_body: dict[str, Any] = {
                    "experiment_key": experiment_key,
                    "experiment_version": version,
                    "subject_id": subject_id,
                }
                if eligible is not None:
                    request_body["eligible"] = eligible
                response = self.transport.request(
                    "POST",
                    "/v1/assign",
                    request_body,
                    timeout_seconds=self.config.request_timeout_seconds,
                    headers={SDK_COMPATIBILITY_HEADER: self.config.schema_version},
                )
                if response.status != 200:
                    error = response.body.get("error", {})
                    raise TransportError(f"{error.get('code', 'assignment_failed')}: {error.get('message', '')}")
                snapshot = self._snapshot(response.body["assignment"], source="service")
        except Exception:
            if cached is not None and self.config.allow_stale_on_error:
                age = max(0.0, now - cached.stored_at)
                return replace(
                    cached.assignment,
                    debug=replace(
                        cached.assignment.debug,
                        source="stale_cache",
                        fallback_status="stale_cache",
                        from_cache=True,
                        cache_age_seconds=age,
                        stale=True,
                    ),
                )
            return AssignmentSnapshot(
                {},
                AssignmentDebug(
                    experiment_key=experiment_key,
                    experiment_version=version,
                    state_id=None,
                    state_key=None,
                    algorithm_version=None,
                    policy_version=None,
                    assignment_probability=0.0,
                    eligible=bool(eligible),
                    source="safe_default",
                    fallback_status="service_unavailable",
                    from_cache=False,
                    cache_age_seconds=None,
                    stale=False,
                    api_schema_version=self.config.schema_version,
                ),
            )

        with self._cache_lock:
            self._cache[cache_key] = _CacheEntry(snapshot, now)
        return snapshot

    def _snapshot(self, payload: Mapping[str, Any], *, source: str) -> AssignmentSnapshot:
        diagnostics = payload.get("diagnostics") or {}
        return AssignmentSnapshot(
            dict(payload.get("values") or payload.get("fallback_values") or {}),
            AssignmentDebug(
                experiment_key=str(payload["experiment_key"]),
                experiment_version=int(payload["experiment_version"]),
                state_id=payload.get("state_id"),
                state_key=payload.get("state_key"),
                algorithm_version=payload.get("algorithm_version"),
                policy_version=payload.get("policy_version"),
                assignment_probability=float(payload["assignment_probability"]),
                eligible=bool(payload["eligible"]),
                source=source,
                fallback_status=payload.get("reason"),
                from_cache=False,
                cache_age_seconds=0.0,
                stale=False,
                bucket=diagnostics.get("bucket"),
                interval_start=diagnostics.get("interval_start"),
                interval_end=diagnostics.get("interval_end"),
                total_weight=diagnostics.get("total_weight"),
                selected_weight=diagnostics.get("selected_weight"),
                eligibility_rule=diagnostics.get("eligibility_rule"),
                eligibility_source=diagnostics.get("eligibility_source"),
                api_schema_version=self.config.schema_version,
            ),
        )

    def _track(self, event: Event) -> TrackingReceipt:
        return self._tracker.enqueue(event)


class ExperimentContext:
    def __init__(
        self,
        *,
        client: VariantGridClient,
        experiment_key: str,
        experiment_version: int,
        subject_id: str,
        context_id: str,
        assignment: AssignmentSnapshot,
    ) -> None:
        self._client = client
        self.experiment_key = experiment_key
        self.experiment_version = experiment_version
        self.subject_id = subject_id
        self.context_id = context_id
        self.assignment = assignment
        self._exposure_sent = False

    @property
    def debug_metadata(self) -> AssignmentDebug:
        return self.assignment.debug

    def get(self, variable: str, default: T) -> T:
        assigned = variable in self.assignment.values
        value = self.assignment.values.get(variable, default)
        if assigned and default is not None and not isinstance(value, type(default)):
            raise TypeError(f"Assigned value for {variable!r} does not match the default type")
        self._record_exposure_once()
        return value  # type: ignore[return-value]

    def goal(self, metric: str, *, idempotency_key: str | None = None) -> TrackingReceipt:
        return self._outcome("goal", metric, 1.0, idempotency_key)

    def increment(
        self, metric: str, amount: float = 1.0, *, idempotency_key: str | None = None
    ) -> TrackingReceipt:
        return self._outcome("goal", metric, float(amount), idempotency_key)

    def observe(
        self, metric: str, value: float, *, idempotency_key: str | None = None
    ) -> TrackingReceipt:
        return self._outcome("observation", metric, float(value), idempotency_key)

    def _record_exposure_once(self) -> None:
        debug = self.assignment.debug
        if self._exposure_sent or debug.state_key is None:
            return
        self._exposure_sent = True
        self._client._track(
            Event(
                event_id=f"exposure:{self.context_id}:{debug.state_id}",
                experiment_key=self.experiment_key,
                experiment_version=self.experiment_version,
                subject_id=self.subject_id,
                event_type="exposure",
                variant_key=debug.state_key,
                occurred_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    def _outcome(
        self, event_type: str, metric: str, value: float, idempotency_key: str | None
    ) -> TrackingReceipt:
        if not metric:
            raise ValueError("metric is required")
        event_id = idempotency_key or uuid4().hex
        return self._client._track(
            Event(
                event_id=event_id,
                experiment_key=self.experiment_key,
                experiment_version=self.experiment_version,
                subject_id=self.subject_id,
                event_type=event_type,
                metric=metric,
                value=value,
                occurred_at=datetime.now(timezone.utc).isoformat(),
            )
        )
