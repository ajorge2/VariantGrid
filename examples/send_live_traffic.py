"""Send SDK traffic to a running VariantGrid dashboard/API service.

Launch the default dashboard experiment first, then run:

    PYTHONPATH=src python3 examples/send_live_traffic.py --subjects 20

The script acts like a deployed product process: it requests assignments over
HTTP, reads a factor (which records exposure), and records deterministic sample
outcomes. The dashboard's event-derived plots update on their next poll.
"""

from __future__ import annotations

import argparse
import json

from variantgrid.sdk import SDKConfig, UrllibTransport, VariantGridClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Send reference product events to VariantGrid")
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    parser.add_argument("--experiment-key", default="onboarding_optimization")
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--variable", default="starter_type")
    parser.add_argument("--default", default="template")
    parser.add_argument("--metric", default="activated_within_7_days")
    parser.add_argument("--subjects", type=int, default=20)
    parser.add_argument("--convert-every", type=int, default=3)
    args = parser.parse_args()
    if args.subjects < 1 or args.convert_every < 1:
        parser.error("subjects and convert-every must be positive")

    client = VariantGridClient(
        SDKConfig(experiment_versions={args.experiment_key: args.version}),
        UrllibTransport(args.base_url),
    )
    assignments: dict[str, int] = {}
    conversions = 0
    try:
        for index in range(args.subjects):
            subject_id = f"reference-product-user-{index:04d}"
            context = client.for_user(args.experiment_key, subject_id, eligible=True)
            context.get(args.variable, args.default)
            state_key = context.debug_metadata.state_key or "safe-default"
            assignments[state_key] = assignments.get(state_key, 0) + 1
            if index % args.convert_every == 0:
                context.goal(
                    args.metric,
                    idempotency_key=(
                        f"reference-conversion:{args.experiment_key}:{args.version}:{subject_id}"
                    ),
                )
                conversions += 1
        flushed = client.flush(timeout_seconds=5)
        if not flushed or client.delivery_failures:
            raise RuntimeError(
                f"event delivery incomplete: flushed={flushed}, failures={client.delivery_failures}"
            )
    finally:
        client.close(timeout_seconds=5)

    print(
        json.dumps(
            {
                "subjects": args.subjects,
                "conversions": conversions,
                "assignments": assignments,
                "dashboard": args.base_url,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
