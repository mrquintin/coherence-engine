"""Outbox dispatcher worker entrypoint."""

from __future__ import annotations

import argparse
import sys

from coherence_engine.server.fund.database import SessionLocal
from coherence_engine.server.fund.services.outbox_dispatcher import OutboxDispatcher, run_loop, topic_prefix_from_env
from coherence_engine.server.fund.services.outbox_publishers import KafkaPublisher, RedisPublisher, SQSPublisher


def _build_publisher(args):
    if args.backend == "kafka":
        if not args.kafka_bootstrap_servers:
            raise ValueError("--kafka-bootstrap-servers is required for kafka backend")
        return KafkaPublisher(bootstrap_servers=args.kafka_bootstrap_servers)
    if args.backend == "sqs":
        if not args.sqs_queue_url:
            raise ValueError("--sqs-queue-url is required for sqs backend")
        return SQSPublisher(queue_url=args.sqs_queue_url, region_name=args.sqs_region)
    if args.backend == "redis":
        if not args.redis_url:
            raise ValueError("--redis-url is required for redis backend")
        return RedisPublisher(redis_url=args.redis_url)
    raise ValueError(f"Unsupported backend: {args.backend}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="coherence-fund-outbox-worker", description="Dispatch outbox events to broker")
    parser.add_argument("--backend", choices=["kafka", "sqs", "redis"], required=True)
    parser.add_argument("--run-mode", choices=["once", "loop"], default="once")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--topic-prefix", type=str, default=topic_prefix_from_env())
    parser.add_argument("--kafka-bootstrap-servers", type=str, default="")
    parser.add_argument("--sqs-queue-url", type=str, default="")
    parser.add_argument("--sqs-region", type=str, default="us-east-1")
    parser.add_argument("--redis-url", type=str, default="")
    args = parser.parse_args()

    try:
        publisher = _build_publisher(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        dispatcher = OutboxDispatcher(db=db, publisher=publisher, topic_prefix=args.topic_prefix)
        if args.run_mode == "once":
            result = dispatcher.dispatch_once(batch_size=args.batch_size)
            print(
                f"Outbox dispatch complete: scanned={result['scanned']} "
                f"published={result['published']} failed={result['failed']}"
            )
        else:
            run_loop(dispatcher, poll_seconds=args.poll_seconds, batch_size=args.batch_size)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

