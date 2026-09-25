from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from banxia_strategy.adapters.wal import SQLiteEventWAL
from banxia_strategy.application.collector import MarketCollector
from banxia_strategy.application.features import RealtimeFeatureProcessor
from banxia_strategy.application.market_sink import MarketSinkWorker
from banxia_strategy.application.projection import ProjectionWorker
from banxia_strategy.application.reliable_publish import ReliableEventPublisher
from banxia_strategy.application.strategy_engine import StrategyEventProcessor
from banxia_strategy.domain.events import EventEnvelope
from banxia_strategy.ports.messaging import ConsumedEvent


STAMP = datetime.fromisoformat("2026-09-24T09:45:00+08:00")


def event(event_type, payload):
    return EventEnvelope.create(
        event_type=event_type,
        producer="test",
        occurred_at=STAMP,
        published_at=STAMP,
        identity={
            "symbol": payload["symbol"],
            "time": payload.get("source_time") or payload.get("bar_time"),
        },
        payload=payload,
        trace_id="trace",
    )


class FakePublisher:
    def __init__(self):
        self.items = []

    def publish(self, topic, key, item):
        self.items.append((topic, key, item))

    def status(self):
        return {"published": len(self.items), "wal_pending": 0}


class FakeSource:
    def fetch(self, codes):
        return {
            code: {
                "quote": {
                    "code": code,
                    "price": 10.6,
                    "open": 10.2,
                    "high": 10.7,
                    "low": 10.1,
                    "last_close": 10.0,
                    "amount": 300000000,
                    "vol": 10000,
                    "bid1": 10.59,
                    "ask1": 10.6,
                    "bid_vol1": 100,
                    "ask_vol1": 50,
                    "servertime": "09:45:00",
                },
                "bars": [],
            }
            for code in codes
        }


class FakeLease:
    def __init__(self, acquired):
        self.acquired = acquired
        self.closed = False

    def try_acquire(self):
        return self.acquired

    def close(self):
        self.closed = True


class CollectorTest(unittest.TestCase):
    def test_collector_publishes_market_facts_without_decisions(self):
        candidate = {
            "code": "002635",
            "name": "安洁科技",
            "industry": "电子",
            "latest_price": 10.0,
            "entry_trigger": "开盘涨幅位于1%～5%",
            "invalidation": "低于0%或高于6%",
            "eligible": True,
            "eligibility_reason": "test",
        }
        with tempfile.TemporaryDirectory() as directory:
            broker = FakePublisher()
            reliable = ReliableEventPublisher(
                wal=SQLiteEventWAL(Path(directory) / "wal.sqlite3"),
                publisher=broker,
            )
            collector = MarketCollector(
                candidates=[candidate],
                publisher=reliable,
                source=FakeSource(),
                clock=lambda: STAMP,
            )
            self.assertEqual(collector.collect_once(), 1)
            published = broker.items[0][2]
            self.assertEqual(published.event_type, "market.quote.snapshot.v1")
            self.assertEqual(published.payload["industry"], "电子")
            self.assertNotIn("advice", published.payload)
            collector.close()

    def test_standby_collector_does_not_contact_market_source(self):
        class FailingSource:
            def fetch(self, _codes):
                raise AssertionError("standby must not fetch")

        lease = FakeLease(False)
        publisher = FakePublisher()
        collector = MarketCollector(
            candidates=[{"code": "002635"}],
            publisher=publisher,
            source=FailingSource(),
            lease=lease,
            clock=lambda: STAMP,
        )
        self.assertEqual(collector.collect_once(), 0)
        self.assertFalse(collector.status()["leader"])
        collector.close()
        self.assertTrue(lease.closed)

    def test_non_trading_day_does_not_contact_market_dependencies(self):
        source = Mock()
        publisher = Mock()
        candidate_loader = Mock()
        session_checker = Mock(return_value=False)
        holiday = datetime.fromisoformat("2026-09-25T09:45:00+08:00")
        collector = MarketCollector(
            candidates=[{"code": "002635"}],
            publisher=publisher,
            source=source,
            clock=lambda: holiday,
            candidate_loader=candidate_loader,
            session_checker=session_checker,
        )

        self.assertEqual(collector.collect_once(), 0)

        session_checker.assert_called_once_with(holiday.date())
        candidate_loader.assert_not_called()
        source.fetch.assert_not_called()
        publisher.replay.assert_not_called()
        self.assertTrue(collector.status()["paused"])
        self.assertEqual(
            collector.status()["pause_reason"],
            "non_trading_day",
        )

    def test_calendar_failure_pauses_collection(self):
        source = Mock()
        collector = MarketCollector(
            candidates=[{"code": "002635"}],
            publisher=Mock(),
            source=source,
            clock=lambda: STAMP,
            session_checker=Mock(
                side_effect=RuntimeError("calendar offline")
            ),
        )

        self.assertEqual(collector.collect_once(), 0)

        source.fetch.assert_not_called()
        self.assertEqual(
            collector.status()["pause_reason"],
            "trading_calendar_unavailable",
        )
        self.assertEqual(collector.status()["last_error"], "calendar offline")


class FeatureProcessorTest(unittest.TestCase):
    def test_volume_window_and_sector_breadth_are_deterministic(self):
        processor = RealtimeFeatureProcessor()
        first = event(
            "market.bar.1m.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "bar_time": STAMP,
                "source_time": STAMP,
                "volume": 100,
            },
        )
        second = event(
            "market.bar.1m.v1",
            {
                **first.payload,
                "bar_time": "2026-09-24T09:46:00+08:00",
                "source_time": "2026-09-24T09:46:00+08:00",
                "volume": 250,
            },
        )
        self.assertIsNone(processor.process(first).payload["minute_volume_ratio"])
        result = processor.process(second)
        self.assertEqual(result.payload["minute_volume_ratio"], 2.5)

        quote = event(
            "market.quote.snapshot.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "industry": "电子",
                "source_time": STAMP,
                "collected_at": STAMP,
                "price": 10.5,
                "previous_close": 10.0,
            },
        )
        result = processor.process(quote)
        self.assertEqual(result.payload["sector_rise_ratio"], 1.0)


class FakeDecisionRepository:
    def __init__(self):
        self.current = None
        self.calls = []

    def get(self, _plan_id, _symbol):
        return self.current

    def apply(self, **kwargs):
        self.calls.append(kwargs)
        self.current = kwargs["decision"]
        return True


class StrategyProcessorTest(unittest.TestCase):
    def test_quote_is_applied_with_real_kafka_coordinates(self):
        repository = FakeDecisionRepository()
        candidate = {
            "code": "002635",
            "name": "安洁科技",
            "latest_price": 10.0,
            "entry_trigger": "开盘涨幅位于1%～5%",
            "invalidation": "低于0%或高于6%",
            "eligible": True,
            "eligibility_reason": "test",
            "plan_date": "2026-09-24",
        }
        processor = StrategyEventProcessor(
            repository=repository,
            plan_id="plan",
            strategy_version_id="version",
            candidates=[candidate],
        )
        source = event(
            "market.quote.snapshot.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "source_time": STAMP,
                "collected_at": STAMP,
                "price": 10.6,
                "open": 10.2,
                "high": 10.7,
                "low": 10.1,
                "previous_close": 10.0,
                "cumulative_volume": 1000,
                "cumulative_amount_cny": 300000000,
                "bid1": 10.59,
                "ask1": 10.6,
                "bid1_volume": 100,
                "ask1_volume": 50,
            },
        )
        consumed = ConsumedEvent(
            "market.quote.snapshot.v1",
            3,
            42,
            "002635",
            source,
        )
        result = processor.process(consumed)

        self.assertEqual(result.payload["state"], "watch")
        self.assertEqual(repository.calls[0]["topic"], consumed.topic)
        self.assertEqual(repository.calls[0]["partition"], 3)
        self.assertEqual(repository.calls[0]["offset"], 42)

    def test_sector_feature_can_hold_an_actionable_quote_at_watch(self):
        repository = FakeDecisionRepository()
        candidate = {
            "code": "002635",
            "name": "安洁科技",
            "latest_price": 10.0,
            "entry_trigger": "开盘涨幅位于0.5%～5.0%",
            "invalidation": "低于-2%或高于7%",
            "eligible": True,
            "eligibility_reason": "test",
            "plan_date": "2026-09-24",
        }
        processor = StrategyEventProcessor(
            repository=repository,
            plan_id="plan",
            strategy_version_id="version-id",
            strategy_version="v1",
            candidates=[candidate],
        )
        feature = event(
            "market.feature.realtime.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "source_time": STAMP,
                "sector_rise_ratio": 0.25,
                "attributes": {"sector_sample_size": 4},
            },
        )
        processor.process(
            ConsumedEvent(feature.event_type, 1, 1, "002635", feature)
        )
        quote = event(
            "market.quote.snapshot.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "source_time": STAMP,
                "collected_at": STAMP,
                "price": 10.99,
                "open": 10.2,
                "high": 10.99,
                "low": 10.1,
                "previous_close": 10.0,
                "cumulative_amount_cny": 300000000,
            },
        )
        result = processor.process(
            ConsumedEvent(quote.event_type, 1, 2, "002635", quote)
        )
        self.assertEqual(result.payload["state"], "watch")
        self.assertEqual(result.payload["label"], "板块确认不足")
        self.assertEqual(result.payload["strategy_version"], "v1")


class FakeConsumer:
    def __init__(self, consumed):
        self.consumed = consumed
        self.committed = []

    def poll(self, _timeout):
        result, self.consumed = self.consumed, None
        return result

    def commit(self, consumed):
        self.committed.append(consumed)


class FakeMarketStore:
    def __init__(self):
        self.quotes = []

    def append_quote_snapshots(self, events):
        self.quotes.extend(events)


class FakeProjectionCache:
    def __init__(self):
        self.quotes = []
        self.stream = []
        self.bars = []

    def set_latest_quote(self, event, ttl):
        self.quotes.append((event, ttl))

    def append_minute_bar(self, event, ttl):
        self.bars.append((event, ttl))

    def append_monitor_event(self, trade_date, event, **kwargs):
        self.stream.append((trade_date, event, kwargs))


class WorkerCommitTest(unittest.TestCase):
    def test_market_sink_commits_only_after_storage(self):
        source = event(
            "market.quote.snapshot.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "source_time": STAMP,
                "collected_at": STAMP,
            },
        )
        consumed = ConsumedEvent(source.event_type, 0, 1, "002635", source)
        consumer = FakeConsumer(consumed)
        store = FakeMarketStore()
        worker = MarketSinkWorker(consumer=consumer, store=store)
        self.assertTrue(worker.run_once())
        self.assertEqual(store.quotes, [source])
        self.assertEqual(consumer.committed, [consumed])

    def test_projection_writes_cache_and_stream_before_commit(self):
        source = event(
            "market.quote.snapshot.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "source_time": STAMP,
                "collected_at": STAMP,
            },
        )
        consumed = ConsumedEvent(source.event_type, 0, 1, "002635", source)
        consumer = FakeConsumer(consumed)
        cache = FakeProjectionCache()
        worker = ProjectionWorker(consumer=consumer, cache=cache)
        self.assertTrue(worker.run_once())
        self.assertEqual(cache.quotes[0][0], source)
        self.assertEqual(cache.stream[0][0], "2026-09-24")
        self.assertEqual(consumer.committed, [consumed])

    def test_projection_persists_minute_bar_before_commit(self):
        source = event(
            "market.bar.1m.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "bar_time": STAMP,
            },
        )
        consumed = ConsumedEvent(source.event_type, 0, 2, "002635", source)
        consumer = FakeConsumer(consumed)
        cache = FakeProjectionCache()
        worker = ProjectionWorker(consumer=consumer, cache=cache)
        self.assertTrue(worker.run_once())
        self.assertEqual(cache.bars[0][0], source)
        self.assertEqual(consumer.committed, [consumed])


if __name__ == "__main__":
    unittest.main()
