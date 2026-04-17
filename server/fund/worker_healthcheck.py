"""Worker readiness/liveness healthcheck entrypoint."""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import text

from coherence_engine.server.fund.database import SessionLocal


def _check_db() -> None:
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    finally:
        db.close()


def _check_kafka(bootstrap_servers: str) -> None:
    try:
        from confluent_kafka.admin import AdminClient
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("confluent-kafka not installed") from exc
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    # Metadata fetch as connectivity check.
    admin.list_topics(timeout=5)


def _check_sqs(queue_url: str, region: str) -> None:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("boto3 not installed") from exc
    client = boto3.client("sqs", region_name=region)
    client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])


def _check_redis(redis_url: str) -> None:
    try:
        import redis
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("redis package not installed") from exc
    client = redis.from_url(redis_url)
    client.ping()


def main() -> int:
    parser = argparse.ArgumentParser(prog="coherence-fund-worker-healthcheck")
    parser.add_argument("--backend", choices=["kafka", "sqs", "redis"], required=True)
    parser.add_argument("--kafka-bootstrap-servers", default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", ""))
    parser.add_argument("--sqs-queue-url", default=os.getenv("SQS_QUEUE_URL", ""))
    parser.add_argument("--sqs-region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", ""))
    args = parser.parse_args()

    try:
        _check_db()
        if args.backend == "kafka":
            if not args.kafka_bootstrap_servers:
                raise RuntimeError("missing kafka bootstrap servers")
            _check_kafka(args.kafka_bootstrap_servers)
        elif args.backend == "sqs":
            if not args.sqs_queue_url:
                raise RuntimeError("missing sqs queue url")
            _check_sqs(args.sqs_queue_url, args.sqs_region)
        else:
            if not args.redis_url:
                raise RuntimeError("missing redis url")
            _check_redis(args.redis_url)
    except Exception as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

