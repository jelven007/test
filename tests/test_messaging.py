from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from banxia_strategy.adapters.kafka import KafkaEventConsumer, KafkaEventPublisher
from banxia_strategy.adapters.lease import PostgresAdvisoryLease
from banxia_strategy.adapters.wal import SQLiteEventWAL, WalCapacityError
from banxia_strategy.application.outbox import OutboxRelay
from banxia_strategy.application.reliable_publish import ReliableEventPublisher
from banxia_strategy.domain.events import EventEnvelope
from banxia_strategy.ports.storage import OutboxRecord


STAMP = datetime.fromisoformat("2026-09-24T09:45:00+08:00")


def quote_event(symbol: str = "002635") -> EventEnvelope:
    return EventEnvelope.create(
        event_type="market.quote.snapshot.v1",
        producer="test-collector",
        occurred_at=STAMP,
        published_at=STAMP,
        identity={"symbol": symbol, "source_time": STAMP},
        payload={
            "trade_date": "2026-09-24",
            "symbol": symbol,
            "source_time": STAMP,
            "collected_at": STAMP,
            "source_node": "test",
        },
        trace_id="trace",
    )


class FakePublisher:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.published = []
        self.closed = False

    def publish(self, topic, key, event):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("broker unavailable")
        self.published.append((topic, key, event))

    def close(self):
        self.closed = True


class EventWALTest(unittest.TestCase):
    def test_append_is_idempotent_and_failure_metadata_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.sqlite3"
            event = quote_event()
            wal = SQLiteEventWAL(path)
            self.assertTrue(wal.append(event.event_type, "002635", event))
            self.assertFalse(wal.append(event.event_type, "002635", event))
            wal.mark_failed(event.event_id, "temporary")
            wal.close()

            reopened = SQLiteEventWAL(path)
            pending = tuple(reopened.pending())
            self.assertEqual(reopened.count(), 1)
            self.assertEqual(pending[0].attempts, 1)
            self.assertEqual(pending[0].last_error, "temporary")
            self.assertEqual(pending[0].event, event)
            reopened.mark_published(event.event_id)
            self.assertEqual(reopened.count(), 0)
            reopened.close()

    def test_capacity_limit_stops_new_events(self):
        with tempfile.TemporaryDirectory() as directory:
            wal = SQLiteEventWAL(
                Path(directory) / "events.sqlite3",
                max_bytes=1,
            )
            with self.assertRaises(WalCapacityError):
                wal.append("market.quote.snapshot.v1", "002635", quote_event())
            wal.close()


class ReliablePublisherTest(unittest.TestCase):
    def test_failure_remains_in_wal_and_replay_removes_after_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            wal = SQLiteEventWAL(Path(directory) / "events.sqlite3")
            broker = FakePublisher(failures=1)
            publisher = ReliableEventPublisher(wal=wal, publisher=broker)
            event = quote_event()

            with self.assertRaisesRegex(RuntimeError, "broker unavailable"):
                publisher.publish(event.event_type, "002635", event)
            self.assertEqual(wal.count(), 1)
            self.assertEqual(publisher.replay(), 1)
            self.assertEqual(wal.count(), 0)
            self.assertEqual(broker.published[0][2], event)
            publisher.close()
            self.assertTrue(broker.closed)


class FakeProducedMessage:
    pass


class FakeKafkaProducer:
    def __init__(self, error=None, remaining=0):
        self.error = error
        self.remaining = remaining
        self.calls = []

    def produce(self, topic, **kwargs):
        self.calls.append((topic, kwargs))
        kwargs["on_delivery"](self.error, FakeProducedMessage())

    def flush(self, _timeout):
        return self.remaining


class FakeKafkaMessage:
    def __init__(self, event, error=None):
        self._event = event
        self._error = error

    def error(self):
        return self._error

    def value(self):
        return json.dumps(self._event.to_dict()).encode()

    def key(self):
        return b"002635"

    def topic(self):
        return "market.quote.snapshot.v1"

    def partition(self):
        return 2

    def offset(self):
        return 17


class FakeKafkaConsumer:
    def __init__(self, message):
        self.message = message
        self.subscriptions = []
        self.commits = []
        self.closed = False

    def subscribe(self, topics):
        self.subscriptions.append(topics)

    def poll(self, _timeout):
        message, self.message = self.message, None
        return message

    def commit(self, **kwargs):
        self.commits.append(kwargs)

    def close(self):
        self.closed = True


class KafkaAdapterTest(unittest.TestCase):
    def test_publisher_waits_for_delivery_ack(self):
        producer = FakeKafkaProducer()
        publisher = KafkaEventPublisher(
            "unused",
            client_id="test",
            producer=producer,
        )
        event = quote_event()
        publisher.publish(event.event_type, "002635", event)
        self.assertEqual(producer.calls[0][0], event.event_type)
        self.assertEqual(producer.calls[0][1]["key"], b"002635")

    def test_consumer_parses_and_commits_exact_message(self):
        raw = FakeKafkaMessage(quote_event())
        client = FakeKafkaConsumer(raw)
        consumer = KafkaEventConsumer(
            "unused",
            group_id="test",
            topics=["market.quote.snapshot.v1"],
            consumer=client,
        )
        consumed = consumer.poll()
        self.assertEqual(consumed.partition, 2)
        self.assertEqual(consumed.offset, 17)
        self.assertEqual(consumed.event.event_type, "market.quote.snapshot.v1")
        consumer.commit(consumed)
        self.assertIs(client.commits[0]["message"], raw)

    def test_delivery_error_is_not_reported_as_success(self):
        producer = FakeKafkaProducer(error="denied")
        publisher = KafkaEventPublisher(
            "unused",
            client_id="test",
            producer=producer,
        )
        with self.assertRaisesRegex(RuntimeError, "denied"):
            publisher.publish("market.quote.snapshot.v1", "002635", quote_event())


class FakeOutboxRepository:
    def __init__(self, records):
        self.records = records
        self.published = []
        self.failed = []

    def claim_outbox(self, worker_id, *, limit, lease_seconds):
        records, self.records = self.records[:limit], self.records[limit:]
        return records

    def mark_outbox_published(self, outbox_id, worker_id):
        self.published.append((outbox_id, worker_id))
        return True

    def mark_outbox_failed(self, outbox_id, worker_id, error):
        self.failed.append((outbox_id, worker_id, error))
        return True


class OutboxRelayTest(unittest.TestCase):
    def test_each_record_is_acknowledged_only_after_publication(self):
        event = quote_event()
        records = [
            OutboxRecord("one", event.event_type, "002635", event, 1),
            OutboxRecord("two", event.event_type, "002635", event, 1),
        ]
        repository = FakeOutboxRepository(records)
        publisher = FakePublisher(failures=1)
        relay = OutboxRelay(
            repository=repository,
            publisher=publisher,
            worker_id="worker",
        )

        self.assertEqual(relay.run_once(), 1)
        self.assertEqual(repository.published, [("two", "worker")])
        self.assertEqual(repository.failed[0][:2], ("one", "worker"))
        self.assertEqual(relay.status()["failed"], 1)


class FakeLeaseCursor:
    def __init__(self, acquired):
        self.acquired = acquired
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return (self.acquired,)


class FakeLeaseConnection:
    def __init__(self, acquired):
        self.closed = False
        self.cursor_instance = FakeLeaseCursor(acquired)

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class AdvisoryLeaseTest(unittest.TestCase):
    def test_only_database_lock_holder_becomes_collector_leader(self):
        connection = FakeLeaseConnection(True)
        lease = PostgresAdvisoryLease(
            lease_key="collector:0",
            connection_factory=lambda: connection,
        )
        self.assertTrue(lease.try_acquire())
        self.assertTrue(lease.try_acquire())
        self.assertIn(
            "pg_try_advisory_lock",
            connection.cursor_instance.calls[0][0],
        )
        self.assertIn("SELECT 1", connection.cursor_instance.calls[1][0])
        lease.close()
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
