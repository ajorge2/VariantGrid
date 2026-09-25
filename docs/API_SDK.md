# VariantGrid API and Python SDK

This is the V1 integration contract. It uses only the Python standard library and delegates assignment and event semantics to VariantGrid's existing core engines.

## Stable HTTP contract

Clients send `X-VariantGrid-Schema-Version: 1`. An incompatible schema receives `426 incompatible_schema`; SDKs then use their configured visible fallback behavior.

| Method | Path | Purpose | Success |
|---|---|---|---|
| `GET` | `/v1/health` | Report API schema compatibility | `200` |
| `POST` | `/v1/assign` | Assign one subject from an immutable running version | `200` |
| `POST` | `/v1/events/batch` | Idempotently accept one or more events | `202` |

`/v1/assign` requires `experiment_key`, `experiment_version`, and `subject_id`. Its response exposes experiment/version, state ID/key, values, algorithm and policy versions, assignment probability, eligibility, and fallback reason.

`/v1/events/batch` requires a non-empty `events` array. Every event must reference a registered experiment version and carry a stable `event_id`. A retry of an accepted event is acknowledged as a duplicate and does not add another stored event.

## Python integration

Run the complete example:

```sh
PYTHONPATH=src python3 examples/reference_integration.py
```

The host application uses three conceptual calls:

```python
experiment = client.for_user("onboarding_optimization", user.id)
starter_type = experiment.get("starter_type", default="template")
experiment.goal("activated", idempotency_key=event.id)
```

`get` records an exposure once per context only when a real state was assigned. `goal`, `increment`, and `observe` enqueue distinct outcome/observation events without blocking on the network. Call `flush` during a graceful worker shutdown; do not put it on a latency-sensitive request path.

## Failure behavior

- Every network operation receives the finite `request_timeout_seconds` value, constrained to 30 seconds or less.
- A fresh cached assignment reports `source="cache"`, its cache age, and `stale=false`.
- If refresh fails and stale use is enabled, the assignment reports `source="stale_cache"`, `fallback_status="stale_cache"`, its age, and `stale=true`.
- If neither service nor cache is available, `get` returns its caller-approved default. Debug metadata reports `source="safe_default"` and `fallback_status="service_unavailable"`; no exposure is emitted.
- Tracking uses a bounded queue. Queue overflow is returned to the caller as `queued=false`; exhausted retries are available through `client.delivery_failures`.
- Retry attempts reuse the original event ID, so an acknowledgement lost after commit cannot double count the event.

## Compatibility and migration

API schema `1` and SDK `0.1.0` are the initial compatibility pair. Additive response fields are permitted within schema 1. Removing, renaming, or changing the meaning of a field requires a new schema path/header version and a period where the service supports both versions. Changing assignment inputs, hashing, or allocation policy requires a new immutable experiment version or policy version; it is not an SDK-only migration.

Before upgrading, run `PYTHONPATH=src python3 -m unittest tests.test_api_sdk -v`. Local/service parity compares the complete assignment wire record from a shared fixture. Roll back the SDK independently when schema 1 remains served; roll back assignment behavior by selecting a previously registered experiment version, never by mutating a launched version.

## Limits

These local tests establish contract behavior, deterministic parity, bounded timeout propagation, cache visibility, and retry idempotency. They do not establish production uptime, distributed ordering, authentication/authorization, network latency, durable background queues, Postgres reliability, or internet-scale throughput. The included WSGI server is a local demonstration adapter, not a hardened production server.
