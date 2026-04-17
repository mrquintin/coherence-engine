"""Outbox publisher backends for Kafka, SQS, and Redis."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Dict


class Publisher(ABC):
    """Abstract publisher contract."""

    @abstractmethod
    def publish(self, topic: str, key: str, payload: Dict[str, object]) -> None:
        """Publish payload to transport."""


class KafkaPublisher(Publisher):
    """Kafka publisher backed by confluent-kafka."""

    def __init__(self, bootstrap_servers: str):
        try:
            from confluent_kafka import Producer
        except ImportError as exc:
            raise ImportError("confluent-kafka is required for Kafka publishing") from exc
        self._producer = Producer({"bootstrap.servers": bootstrap_servers})

    def publish(self, topic: str, key: str, payload: Dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._producer.produce(topic=topic, key=key.encode("utf-8"), value=data)
        self._producer.flush()


class SQSPublisher(Publisher):
    """SQS publisher backed by boto3."""

    def __init__(self, queue_url: str, region_name: str = "us-east-1"):
        try:
            import boto3
        except ImportError as exc:
            raise ImportError("boto3 is required for SQS publishing") from exc
        self._queue_url = queue_url
        self._client = boto3.client("sqs", region_name=region_name)

    def publish(self, topic: str, key: str, payload: Dict[str, object]) -> None:
        self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps(payload),
            MessageAttributes={
                "event_type": {"DataType": "String", "StringValue": topic},
                "event_key": {"DataType": "String", "StringValue": key},
            },
        )


class RedisPublisher(Publisher):
    """Redis publisher using pub/sub channels."""

    def __init__(self, redis_url: str):
        try:
            import redis
        except ImportError as exc:
            raise ImportError("redis is required for Redis publishing") from exc
        self._client = redis.from_url(redis_url)

    def publish(self, topic: str, key: str, payload: Dict[str, object]) -> None:
        envelope = {
            "key": key,
            "payload": payload,
        }
        self._client.publish(topic, json.dumps(envelope))

