from __future__ import annotations

from dataclasses import replace
import unittest

from variantgrid.api import (
    API_SCHEMA_VERSION,
    SDK_COMPATIBILITY_HEADER,
    APIRequest,
    APIResponse,
    VariantGridAPI,
    assignment_payload,
)
from variantgrid.assignment import AssignmentEngine
from variantgrid.events import EventStore
from variantgrid.models import Experiment, Factor
from variantgrid.registry import ExperimentRegistry, Lifecycle
from variantgrid.sdk import (
    InProcessTransport,
    SDKConfig,
    TransportError,
    VariantGridClient,
)


def running_system() -> tuple[Experiment, ExperimentRegistry, EventStore, VariantGridAPI]:
    experiment = Experiment.from_factors(
        key="onboarding",
        version=1,
        factors=(Factor("starter_type", ("template", "guided")),),
        primary_metric="activated",
        salt="api-sdk-test",
    )
    registry = ExperimentRegistry()
    registry.create(experiment)
    registry.transition("onboarding", 1, Lifecycle.RUNNING)
    store = EventStore()
    return experiment, registry, store, VariantGridAPI(registry, store)


def close_if_supported(store: EventStore) -> None:
    close = getattr(store, "close", None)
    if close is not None:
        close()


class SwitchableTransport:
    def __init__(self, delegate: InProcessTransport) -> None:
        self.delegate = delegate
        self.fail = False
        self.timeouts: list[float] = []

    def request(self, method, path, body, *, timeout_seconds, headers):
        self.timeouts.append(timeout_seconds)
        if self.fail:
            raise TransportError("service unavailable")
        return self.delegate.request(
            method, path, body, timeout_seconds=timeout_seconds, headers=headers
        )


class AlwaysFailTransport:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def request(self, method, path, body, *, timeout_seconds, headers):
        self.timeouts.append(timeout_seconds)
        raise TimeoutError("bounded simulated timeout")


class AckLostTransport:
    """Simulate a server commit whose acknowledgement is lost in transit."""

    def __init__(self, delegate: InProcessTransport) -> None:
        self.delegate = delegate
        self.lost_once = False

    def request(self, method, path, body, *, timeout_seconds, headers):
        response = self.delegate.request(
            method, path, body, timeout_seconds=timeout_seconds, headers=headers
        )
        if path == "/v1/events/batch" and not self.lost_once:
            self.lost_once = True
            raise TimeoutError("acknowledgement lost after commit")
        return response


class APISDKTests(unittest.TestCase):
    def config(self, **changes) -> SDKConfig:
        return replace(
            SDKConfig(
                experiment_versions={"onboarding": 1},
                request_timeout_seconds=0.05,
                tracking_retry_backoff_seconds=0,
            ),
            **changes,
        )

    def test_service_and_local_evaluator_have_complete_assignment_parity(self) -> None:
        experiment, _, store, api = running_system()
        expected = assignment_payload(AssignmentEngine(experiment).assign_with_metadata("user-42"))
        service = api.handle(
            APIRequest(
                "POST",
                "/v1/assign",
                {
                    "experiment_key": "onboarding",
                    "experiment_version": 1,
                    "subject_id": "user-42",
                },
                {SDK_COMPATIBILITY_HEADER: API_SCHEMA_VERSION},
            )
        )
        self.assertEqual(service.status, 200)
        self.assertEqual(service.body["assignment"], expected)

        client = VariantGridClient(
            self.config(),
            InProcessTransport(api),
            local_experiments={("onboarding", 1): experiment},
        )
        self.addCleanup(client.close)
        local = client.for_user("onboarding", "user-42")
        self.assertEqual(local.assignment.values, expected["values"])
        self.assertEqual(local.debug_metadata.state_id, expected["state_id"])
        self.assertEqual(local.debug_metadata.state_key, expected["state_key"])
        self.assertEqual(local.debug_metadata.policy_version, expected["policy_version"])
        self.assertEqual(
            local.debug_metadata.assignment_probability, expected["assignment_probability"]
        )
        close_if_supported(store)

    def test_repeated_service_assignment_records_one_idempotent_assignment_event(self) -> None:
        _, _, store, api = running_system()
        request = APIRequest(
            "POST",
            "/v1/assign",
            {
                "experiment_key": "onboarding",
                "experiment_version": 1,
                "subject_id": "repeat-user",
            },
            {SDK_COMPATIBILITY_HEADER: API_SCHEMA_VERSION},
        )

        first = api.handle(request)
        second = api.handle(request)

        self.assertEqual(200, first.status)
        self.assertEqual(first.body["assignment"], second.body["assignment"])
        self.assertEqual(1, store.event_count("assignment"))
        close_if_supported(store)

    def test_outage_returns_visible_safe_default(self) -> None:
        transport = AlwaysFailTransport()
        client = VariantGridClient(self.config(), transport)
        self.addCleanup(client.close)
        context = client.for_user("onboarding", "user-1")
        self.assertEqual(context.get("starter_type", default="template"), "template")
        self.assertEqual(context.debug_metadata.source, "safe_default")
        self.assertEqual(context.debug_metadata.fallback_status, "service_unavailable")
        self.assertFalse(context.debug_metadata.from_cache)

    def test_expired_cache_is_visible_when_service_fails(self) -> None:
        _, _, store, api = running_system()
        now = [100.0]
        transport = SwitchableTransport(InProcessTransport(api))
        client = VariantGridClient(
            self.config(cache_ttl_seconds=10), transport, clock=lambda: now[0]
        )
        self.addCleanup(client.close)
        first = client.for_user("onboarding", "user-1")
        self.assertEqual(first.debug_metadata.source, "service")
        now[0] += 11
        transport.fail = True
        stale = client.for_user("onboarding", "user-1")
        self.assertEqual(stale.assignment.values, first.assignment.values)
        self.assertTrue(stale.debug_metadata.stale)
        self.assertTrue(stale.debug_metadata.from_cache)
        self.assertEqual(stale.debug_metadata.cache_age_seconds, 11)
        self.assertEqual(stale.debug_metadata.fallback_status, "stale_cache")
        close_if_supported(store)

    def test_timeouts_are_bounded_for_assignment_and_tracking(self) -> None:
        _, _, store, api = running_system()
        transport = SwitchableTransport(InProcessTransport(api))
        client = VariantGridClient(self.config(request_timeout_seconds=0.037), transport)
        self.addCleanup(client.close)
        context = client.for_user("onboarding", "user-1", context_id="request-1")
        context.get("starter_type", default="template")
        context.goal("activated", idempotency_key="goal-1")
        self.assertTrue(client.flush())
        self.assertGreaterEqual(len(transport.timeouts), 3)
        self.assertEqual(set(transport.timeouts), {0.037})
        close_if_supported(store)

    def test_retry_after_lost_ack_does_not_double_count(self) -> None:
        _, _, store, api = running_system()
        client = VariantGridClient(self.config(), AckLostTransport(InProcessTransport(api)))
        self.addCleanup(client.close)
        context = client.for_user("onboarding", "user-1")
        receipt = context.goal("activated", idempotency_key="stable-goal-id")
        self.assertTrue(receipt.queued)
        self.assertTrue(client.flush())
        self.assertEqual(store.event_count("assignment"), 1)
        self.assertEqual(store.event_count("goal"), 1)
        self.assertEqual(store.event_count(), 2)
        self.assertEqual(client.delivery_failures, ())
        close_if_supported(store)

    def test_get_records_only_one_exposure_per_context(self) -> None:
        _, _, store, api = running_system()
        client = VariantGridClient(self.config(), InProcessTransport(api))
        self.addCleanup(client.close)
        context = client.for_user("onboarding", "user-1", context_id="page-view-1")
        context.get("starter_type", default="template")
        context.get("starter_type", default="template")
        self.assertTrue(client.flush())
        self.assertEqual(store.event_count("assignment"), 1)
        self.assertEqual(store.event_count("exposure"), 1)
        self.assertEqual(store.event_count(), 2)
        close_if_supported(store)

    def test_client_schema_mismatch_is_rejected_and_falls_back_visibly(self) -> None:
        _, _, store, api = running_system()
        incompatible = self.config(schema_version="999")
        client = VariantGridClient(incompatible, InProcessTransport(api))
        self.addCleanup(client.close)
        context = client.for_user("onboarding", "user-1")
        self.assertEqual(context.debug_metadata.source, "safe_default")
        self.assertEqual(context.debug_metadata.fallback_status, "service_unavailable")
        close_if_supported(store)

    def test_api_rejects_events_for_unknown_versions(self) -> None:
        _, _, store, api = running_system()
        response = api.handle(
            APIRequest(
                "POST",
                "/v1/events/batch",
                {
                    "events": [
                        {
                            "event_id": "e-1",
                            "experiment_key": "onboarding",
                            "experiment_version": 99,
                            "subject_id": "user-1",
                            "event_type": "goal",
                            "metric": "activated",
                            "value": 1,
                        }
                    ]
                },
            )
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(store.event_count(), 0)
        close_if_supported(store)


if __name__ == "__main__":
    unittest.main()
