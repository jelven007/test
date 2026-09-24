from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional

from ..domain.events import EventEnvelope
from ..ports.messaging import ConsumedEvent


def _json_bytes(event: EventEnvelope) -> bytes:
    return json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class KafkaEventPublisher:
    """Synchronous acknowledgement wrapper around confluent-kafka."""

    def __init__(
        self,
        bootstrap_servers: str,
        *,
        client_id: str,
        producer: Any = None,
        delivery_timeout_seconds: float = 10.0,
        extra_config: Optional[Mapping[str, Any]] = None,
    ):
        if producer is None:
            try:
                from confluent_kafka import Producer
            except ImportError as exc:
                raise RuntimeError(
                    "Kafka adapter requires `pip install -e '.[production]'`"
                ) from exc
            config = {
                "bootstrap.servers": bootstrap_servers,
                "client.id": client_id,
                "enable.idempotence": True,
                "acks": "all",
                "retries": 2_147_483_647,
                "max.in.flight.requests.per.connection": 5,
            }
            config.update(extra_config or {})
            producer = Producer(config)
        self.producer = producer
        self.delivery_timeout_seconds = delivery_timeout_seconds

    def publish(self, topic: str, key: str, event: EventEnvelope) -> None:
        delivery_error = []

        def delivered(error: Any, _message: Any) -> None:
            if error is not None:
                delivery_error.append(str(error))

        self.producer.produce(
            topic,
            key=key.encode("utf-8"),
            value=_json_bytes(event),
            headers={
                "event_type": event.event_type,
                "schema_version": str(event.schema_version),
                "trace_id": event.trace_id,
            },
            on_delivery=delivered,
        )
        remaining = self.producer.flush(self.delivery_timeout_seconds)
        if remaining:
            raise TimeoutError(
                f"Kafka did not acknowledge {remaining} message(s)"
            )
        if delivery_error:
            raise RuntimeError(f"Kafka publication failed: {delivery_error[0]}")

    def ready(self) -> bool:
        try:
            self.producer.list_topics(timeout=3)
            return True
        except Exception:
            return False

    def close(self) -> None:
        self.producer.flush(self.delivery_timeout_seconds)


class KafkaEventConsumer:
    """Manual-commit consumer that rejects malformed event envelopes."""

    def __init__(
        self,
        bootstrap_servers: str,
        *,
        group_id: str,
        topics: Iterable[str],
        consumer: Any = None,
        extra_config: Optional[Mapping[str, Any]] = None,
    ):
        if consumer is None:
            try:
                from confluent_kafka import Consumer
            except ImportError as exc:
                raise RuntimeError(
                    "Kafka adapter requires `pip install -e '.[production]'`"
                ) from exc
            config = {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
                "isolation.level": "read_committed",
            }
            config.update(extra_config or {})
            consumer = Consumer(config)
        self.consumer = consumer
        self.consumer.subscribe(list(topics))

    def poll(self, timeout: float = 1.0) -> Optional[ConsumedEvent]:
        message = self.consumer.poll(timeout)
        if message is None:
            return None
        error = message.error()
        if error is not None:
            raise RuntimeError(f"Kafka consume failed: {error}")
        try:
            payload = json.loads(message.value().decode("utf-8"))
            event = EventEnvelope.from_dict(payload)
        except Exception as exc:
            raise ValueError(
                f"invalid event at {message.topic()}:{message.partition()}:{message.offset()}: {exc}"
            ) from exc
        raw_key = message.key()
        key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key or "")
        return ConsumedEvent(
            topic=message.topic(),
            partition=message.partition(),
            offset=message.offset(),
            key=key,
            event=event,
            raw=message,
        )

    def commit(self, event: ConsumedEvent) -> None:
        if event.raw is None:
            raise ValueError("consumed event does not contain the broker message")
        self.consumer.commit(message=event.raw, asynchronous=False)

    def close(self) -> None:
        self.consumer.close()
