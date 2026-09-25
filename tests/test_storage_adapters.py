from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from banxia_strategy.adapters.clickhouse import (
    BAR_COLUMNS,
    QUOTE_COLUMNS,
    ClickHouseMarketHistoryStore,
)
from banxia_strategy.adapters.minio import MinioObjectAssetStore
from banxia_strategy.adapters.postgres import PostgresStorage
from banxia_strategy.adapters.redis import RedisSnapshotCache
from banxia_strategy.application.persistence import (
    AsyncMarketPersistence,
    persist_report_copy,
)
from banxia_strategy.domain.events import EventEnvelope
from banxia_strategy.ports.storage import (
    DecisionRecord,
    ReportAsset,
    ReportIdentity,
)
from banxia_strategy.storage_config import StorageSettings


STAMP = datetime.fromisoformat("2026-09-24T09:45:00+08:00")


def event(event_type, payload, identity=None):
    return EventEnvelope.create(
        event_type=event_type,
        producer="test",
        occurred_at=STAMP,
        published_at=STAMP,
        identity=identity or {
            "symbol": payload["symbol"],
            "time": payload.get("source_time") or payload.get("bar_time"),
        },
        payload=payload,
        trace_id="trace",
    )


class FakeClickHouseClient:
    def __init__(self):
        self.inserts = []
        self.closed = False

    def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))

    def close(self):
        self.closed = True


class ClickHouseAdapterTest(unittest.TestCase):
    def test_quote_and_bar_events_are_mapped_to_schema_columns(self):
        client = FakeClickHouseClient()
        store = ClickHouseMarketHistoryStore(client=client)
        quote = event(
            "market.quote.snapshot.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "source_time": STAMP,
                "collected_at": STAMP,
                "price": "17.50",
                "open": "17.10",
                "high": "17.60",
                "low": "17.00",
                "previous_close": "16.79",
                "cumulative_volume": 1234,
                "cumulative_amount_cny": "500000000",
                "bid1": "17.49",
                "bid1_volume": 100,
                "ask1": "17.50",
                "ask1_volume": 50,
                "source_node": "mootdx-a",
            },
        )
        bar = event(
            "market.bar.1m.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "bar_time": STAMP,
                "open": "17.40",
                "high": "17.60",
                "low": "17.30",
                "close": "17.50",
                "volume": 500,
                "amount_cny": "8750",
                "source_time": STAMP,
                "collected_at": STAMP,
                "revision": 7,
            },
        )

        store.append_quote_snapshots([quote])
        store.upsert_minute_bars([bar])

        self.assertEqual(client.inserts[0][0], "banxia.market_quote_snapshot")
        self.assertEqual(client.inserts[0][2], list(QUOTE_COLUMNS))
        self.assertEqual(client.inserts[0][1][0][1], "002635")
        self.assertEqual(client.inserts[1][0], "banxia.market_bar_1m")
        self.assertEqual(client.inserts[1][2], list(BAR_COLUMNS))
        self.assertEqual(client.inserts[1][1][0][-1], 7)

    def test_wrong_event_type_is_rejected(self):
        store = ClickHouseMarketHistoryStore(client=FakeClickHouseClient())
        wrong = event(
            "market.bar.1m.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "bar_time": STAMP,
            },
        )
        with self.assertRaises(ValueError):
            store.append_quote_snapshots([wrong])


class FakeRedisClient:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.sorted_values = {}

    def set(self, key, value, ex):
        self.values[key] = value
        self.calls.append((key, value, ex))

    def get(self, key):
        return self.values.get(key)

    def pipeline(self, transaction=True):
        return self

    def zadd(self, key, values):
        bucket = self.sorted_values.setdefault(key, {})
        bucket.update(values)
        return self

    def zremrangebyrank(self, _key, _start, _stop):
        return self

    def expire(self, _key, _ttl):
        return self

    def execute(self):
        return []

    def zrange(self, key, _start, _stop):
        bucket = self.sorted_values.get(key, {})
        return [
            value
            for value, _score in sorted(
                bucket.items(),
                key=lambda item: item[1],
            )
        ]


class RedisAdapterTest(unittest.TestCase):
    def test_snapshot_round_trip_uses_explicit_ttl(self):
        client = FakeRedisClient()
        cache = RedisSnapshotCache(client=client)
        cache.set_monitor_snapshot("2026-09-24", {"revision": 3}, 90)
        self.assertEqual(client.calls[0][2], 90)
        self.assertEqual(
            client.calls[0][0],
            "banxia:monitor:snapshot:2026-09-24",
        )
        self.assertEqual(
            cache.get_monitor_snapshot("2026-09-24"),
            {"revision": 3},
        )
        with self.assertRaises(ValueError):
            cache.set_monitor_snapshot("2026-09-24", {}, 0)

    def test_feature_projection_merges_independent_flink_outputs(self):
        client = FakeRedisClient()
        cache = RedisSnapshotCache(client=client)
        sector = event(
            "market.feature.realtime.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "sector_rise_ratio": 0.75,
                "minute_volume_ratio": None,
                "attributes": {"sector_sample_size": 4},
            },
        )
        volume = event(
            "market.feature.realtime.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "sector_rise_ratio": None,
                "minute_volume_ratio": 2.5,
                "attributes": {},
            },
            identity={"symbol": "002635", "feature": "volume"},
        )
        cache.set_latest_feature(sector, 90)
        cache.set_latest_feature(volume, 90)
        payload = cache.get_latest_feature("002635")["payload"]
        self.assertEqual(payload["sector_rise_ratio"], 0.75)
        self.assertEqual(payload["minute_volume_ratio"], 2.5)

    def test_minute_bar_projection_round_trip(self):
        client = FakeRedisClient()
        cache = RedisSnapshotCache(client=client)
        bar = event(
            "market.bar.1m.v1",
            {
                "trade_date": "2026-09-24",
                "symbol": "002635",
                "bar_time": STAMP,
            },
        )
        cache.append_minute_bar(bar, 90)
        self.assertEqual(tuple(cache.get_minute_bars("002635")), (bar.to_dict(),))

    def test_strategy_cleanup_only_removes_matching_monitor_data(self):
        client = Mock()
        client.scan_iter.return_value = [b"banxia:decision:latest:plan-1:002635"]
        client.delete.side_effect = [1, 1]
        client.get.return_value = json.dumps(
            {"strategy_id": "strategy-1", "plan_id": "plan-1"}
        )
        client.xrange.return_value = [
            (
                b"1-0",
                {
                    b"payload": json.dumps(
                        {"payload": {"plan_id": "plan-1"}}
                    ).encode()
                },
            ),
            (
                b"2-0",
                {
                    b"payload": json.dumps(
                        {"payload": {"plan_id": "other-plan"}}
                    ).encode()
                },
            ),
        ]
        client.xdel.return_value = 1
        cache = RedisSnapshotCache(client=client)

        result = cache.delete_strategy_data(
            "strategy-1",
            ["plan-1"],
            ["2026-09-24"],
        )

        self.assertEqual(
            result,
            {"decisions": 1, "snapshots": 1, "stream_events": 1},
        )
        client.xdel.assert_called_once_with(
            "banxia:stream:monitor:2026-09-24",
            b"1-0",
        )


class FakeMinioClient:
    def __init__(self):
        self.calls = []

    def put_object(
        self,
        bucket,
        object_key,
        data,
        *,
        length,
        content_type,
        metadata,
    ):
        self.calls.append(
            {
                "bucket": bucket,
                "object_key": object_key,
                "content": data.read(),
                "length": length,
                "content_type": content_type,
                "metadata": metadata,
            }
        )

    def list_objects(self, _bucket, **_kwargs):
        return ()

    def remove_object(self, bucket, object_key, version_id=None):
        self.calls.append({
            "bucket": bucket,
            "removed": object_key,
            "version_id": version_id,
        })


class MinioAdapterTest(unittest.TestCase):
    def test_put_returns_content_hash_and_object_metadata(self):
        client = FakeMinioClient()
        store = MinioObjectAssetStore(
            "unused",
            "unused",
            "unused",
            client=client,
        )
        content = b"# report\n"
        asset = store.put(
            object_key="strategy_version=v1/trade_date=2026-09-24/report.md",
            content=content,
            content_type="text/markdown",
            metadata={"report_id": "run-1", "format": "markdown"},
        )
        digest = hashlib.sha256(content).hexdigest()
        self.assertEqual(asset.content_hash, digest)
        self.assertEqual(asset.content_type, "text/markdown")
        self.assertEqual(asset.size_bytes, len(content))
        self.assertEqual(client.calls[0]["metadata"]["sha256"], digest)
        with self.assertRaises(ValueError):
            store.put(
                object_key="../report.md",
                content=content,
                content_type="text/markdown",
            )

    def test_remove_objects_validates_and_deduplicates_keys(self):
        client = FakeMinioClient()
        store = MinioObjectAssetStore(
            "unused",
            "unused",
            "unused",
            client=client,
        )

        self.assertEqual(store.remove_objects(["a/report.md", "a/report.md"]), 1)
        self.assertEqual(client.calls, [{
            "bucket": "strategy-reports",
            "removed": "a/report.md",
            "version_id": None,
        }])
        with self.assertRaises(ValueError):
            store.remove_objects(["../other"])

    def test_remove_objects_purges_all_versions(self):
        client = Mock()
        client.list_objects.return_value = [
            SimpleNamespace(object_name="a/report.md", version_id="v2"),
            SimpleNamespace(object_name="a/report.md", version_id="v1"),
            SimpleNamespace(object_name="a/report.md.bak", version_id="other"),
        ]
        store = MinioObjectAssetStore(
            "unused",
            "unused",
            "unused",
            client=client,
        )

        self.assertEqual(store.remove_objects(["a/report.md"]), 1)
        self.assertEqual(
            client.remove_object.call_args_list,
            [
                call(
                    "strategy-reports",
                    "a/report.md",
                    version_id="v2",
                ),
                call(
                    "strategy-reports",
                    "a/report.md",
                    version_id="v1",
                ),
            ],
        )


class FakeCursor:
    def __init__(self, fetch_results):
        self.fetch_results = list(fetch_results)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.fetch_results.pop(0)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class PostgresAdapterTest(unittest.TestCase):
    def test_report_refresh_jobs_are_enqueued_claimed_and_completed(self):
        created_at = datetime.fromisoformat("2026-09-25T09:00:00+08:00")
        started_at = datetime.fromisoformat("2026-09-25T09:00:01+08:00")
        finished_at = datetime.fromisoformat("2026-09-25T09:00:02+08:00")
        payload = {"trade_date": "2026-09-24", "requested_by": "request-1"}
        cursor = FakeCursor(
            [
                ("job-1", "queued", payload, created_at),
                ("job-1", "running", 1, payload, created_at, started_at),
                (
                    "job-1",
                    "report_refresh",
                    "succeeded",
                    1,
                    payload,
                    {"trade_date": "2026-09-24"},
                    None,
                    created_at,
                    started_at,
                    finished_at,
                ),
            ]
        )
        storage = PostgresStorage(
            connection_factory=lambda: FakeConnection(cursor)
        )

        queued = storage.enqueue_report_refresh(
            "2026-09-24",
            requested_by="request-1",
        )
        claimed = storage.claim_report_refresh()
        storage.finish_report_refresh(
            "job-1",
            succeeded=True,
            result={"trade_date": "2026-09-24"},
        )
        completed = storage.get_job_execution("job-1")

        self.assertEqual(queued["status"], "queued")
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(completed["status"], "succeeded")
        statements = "\n".join(call[0] for call in cursor.calls)
        self.assertIn("INSERT INTO banxia.job_execution", statements)
        self.assertIn("FOR UPDATE SKIP LOCKED", statements)
        self.assertIn("INTERVAL '15 minutes'", statements)
        self.assertIn("SET status = %s", statements)

    def test_report_transaction_writes_plan_candidate_and_asset_metadata(self):
        ids = [
            ("11111111-1111-1111-1111-111111111111",),
            ("22222222-2222-2222-2222-222222222222",),
            ("33333333-3333-3333-3333-333333333333",),
            ("44444444-4444-4444-4444-444444444444",),
        ]
        cursor = FakeCursor(ids)
        storage = PostgresStorage(
            connection_factory=lambda: FakeConnection(cursor)
        )
        report = {
            "as_of": "2026-09-23",
            "next_session": "2026-09-24",
            "generated_at": "2026-09-23T16:20:00+08:00",
            "data_source": "mootdx",
            "data_sessions": ["2026-09-23"],
            "market": {"regime": "active"},
            "rejected_count": 2,
            "candidates": [
                {
                    "code": "002635",
                    "name": "安洁科技",
                    "industry": "电子",
                    "rank": 1,
                    "score": 80.0,
                    "strategy": "一进二试错",
                    "latest_price": 16.79,
                    "amount_cny": 500000000,
                    "turnover_pct": 8.0,
                    "float_market_cap_cny": 8000000000,
                    "reasons": ["test"],
                    "entry_trigger": "test",
                    "invalidation": "test",
                    "exit_plan": "test",
                    "position_limit_pct": 20,
                }
            ],
        }
        asset = ReportAsset(
            report_id="run",
            format="json",
            object_key="reports/hash/candidates.json",
            content_hash="a" * 64,
            content_type="application/json",
            size_bytes=12,
        )

        identity = storage.persist_report(
            report,
            strategy_version="v1",
            strategy_config={"minimum_amount_cny": 200000000},
            code_commit="abc123",
            assets=[asset],
        )

        self.assertEqual(
            identity.plan_id,
            "44444444-4444-4444-4444-444444444444",
        )
        statements = "\n".join(call[0] for call in cursor.calls)
        self.assertIn("INSERT INTO banxia.strategy_run", statements)
        self.assertIn("SET status = 'expired'", statements)
        self.assertIn("DELETE FROM banxia.candidate", statements)
        self.assertIn("INSERT INTO banxia.report_asset", statements)
        asset_params = cursor.calls[-1][1]
        self.assertEqual(asset_params[2], asset.object_key)
        self.assertEqual(asset_params[4], "application/json")

    def test_duplicate_decision_input_returns_false_before_state_write(self):
        cursor = FakeCursor([None])
        storage = PostgresStorage(
            connection_factory=lambda: FakeConnection(cursor)
        )
        decision_event = event(
            "strategy.decision.v1",
            {
                "symbol": "002635",
                "plan_id": "plan",
            },
        )
        result = storage.apply(
            input_event_id="event-1",
            decision=SimpleNamespace(plan_id="plan"),
            outbox_event=decision_event,
        )
        self.assertFalse(result)
        self.assertEqual(len(cursor.calls), 1)
        self.assertIn("ON CONFLICT DO NOTHING", cursor.calls[0][0])

    def test_report_and_plan_events_are_enqueued_idempotently(self):
        cursor = FakeCursor(
            [
                ("11111111-1111-1111-1111-111111111111",),
                ("22222222-2222-2222-2222-222222222222",),
                ("33333333-3333-3333-3333-333333333333",),
                ("44444444-4444-4444-4444-444444444444",),
                ("55555555-5555-5555-5555-555555555555",),
            ]
        )
        storage = PostgresStorage(
            connection_factory=lambda: FakeConnection(cursor)
        )
        storage.persist_report(
            {
                "as_of": "2026-09-23",
                "next_session": "2026-09-24",
                "generated_at": "2026-09-23T16:20:00+08:00",
                "candidates": [],
            },
            strategy_version="v1",
            strategy_config={},
            code_commit="abc123",
            enqueue_events=True,
        )
        outbox_calls = [
            call
            for call in cursor.calls
            if "INSERT INTO banxia.outbox_event" in call[0]
        ]
        self.assertEqual(len(outbox_calls), 2)
        self.assertEqual(
            {call[1][4] for call in outbox_calls},
            {"strategy.plan.created.v1", "report.generated.v1"},
        )
        self.assertTrue(
            all("ON CONFLICT (event_id)" in call[0] for call in outbox_calls)
        )
        watchlist_index = next(index for index, call in enumerate(cursor.calls) if "INSERT INTO banxia.watchlist" in call[0])
        event_index = next(index for index, call in enumerate(cursor.calls) if "INSERT INTO banxia.outbox_event" in call[0])
        self.assertLess(watchlist_index, event_index)

    def test_report_update_at_a_later_time_gets_new_event_ids(self):
        cursor = FakeCursor([])
        storage = PostgresStorage(
            connection_factory=lambda: FakeConnection(cursor)
        )
        identity = ReportIdentity(
            run_id="33333333-3333-3333-3333-333333333333",
            strategy_version_id="22222222-2222-2222-2222-222222222222",
            plan_id="44444444-4444-4444-4444-444444444444",
        )
        report = {
            "as_of": "2026-09-23",
            "next_session": "2026-09-24",
            "candidates": [],
        }

        for generated_at in (
            datetime.fromisoformat("2026-09-23T16:30:00+08:00"),
            datetime.fromisoformat("2026-09-23T23:30:00+08:00"),
        ):
            storage._enqueue_report_events(
                cursor,
                report=report,
                identity=identity,
                strategy_version="v1",
                assets=(),
                occurred_at=generated_at,
            )

        event_ids = [
            call[1][0]
            for call in cursor.calls
            if "INSERT INTO banxia.outbox_event" in call[0]
        ]
        self.assertEqual(len(event_ids), 4)
        self.assertEqual(len(set(event_ids)), 4)

    def test_decision_state_history_and_outbox_share_one_transaction(self):
        cursor = FakeCursor([("event-1",), None])
        storage = PostgresStorage(
            connection_factory=lambda: FakeConnection(cursor)
        )
        decision_event = event(
            "strategy.decision.v1",
            {
                "symbol": "002635",
                "plan_id": "11111111-1111-1111-1111-111111111111",
                "rule_inputs": {"price": 17.5},
            },
        )
        decision = DecisionRecord(
            plan_id="11111111-1111-1111-1111-111111111111",
            symbol="002635",
            state="watch",
            reason_code="watch",
            reason="等待封板",
            source_event_id="event-1",
            strategy_version="22222222-2222-2222-2222-222222222222",
            irreversible=False,
            occurred_at=STAMP,
            version=1,
        )

        self.assertTrue(
            storage.apply(
                input_event_id="event-1",
                decision=decision,
                outbox_event=decision_event,
            )
        )
        statements = [call[0] for call in cursor.calls]
        self.assertEqual(len(statements), 6)
        self.assertIn("INSERT INTO banxia.decision_state", statements[2])
        self.assertIn("INSERT INTO banxia.decision_event", statements[3])
        self.assertIn("INSERT INTO banxia.outbox_event", statements[4])
        self.assertEqual(cursor.calls[2][1][2], "watch")
        self.assertEqual(cursor.calls[4][1][2], "strategy.decision.v1")

    def test_decision_inbox_uses_real_partition_and_offset(self):
        cursor = FakeCursor([None])
        storage = PostgresStorage(
            connection_factory=lambda: FakeConnection(cursor)
        )
        decision_event = event(
            "strategy.decision.v1",
            {"symbol": "002635", "plan_id": "plan"},
        )
        storage.apply(
            input_event_id="event-1",
            decision=SimpleNamespace(plan_id="plan"),
            outbox_event=decision_event,
            topic="market.quote.snapshot.v1",
            partition=7,
            offset=42,
        )
        self.assertEqual(cursor.calls[0][1][2], "market.quote.snapshot.v1:plan:plan")
        self.assertEqual(cursor.calls[0][1][3:], (7, 42))


class FakeMarketStore:
    def __init__(self, fail=False):
        self.fail = fail
        self.quotes = []
        self.bars = []
        self.closed = False

    def append_quote_snapshots(self, events):
        self.quotes.extend(events)
        if self.fail:
            raise RuntimeError("clickhouse unavailable")

    def upsert_minute_bars(self, events):
        self.bars.extend(events)

    def close(self):
        self.closed = True


class FakeSnapshotCache:
    def __init__(self):
        self.calls = []
        self.closed = False

    def set_monitor_snapshot(self, trade_date, snapshot, ttl_seconds):
        self.calls.append((trade_date, snapshot, ttl_seconds))

    def close(self):
        self.closed = True


class FakeDecisionRepository:
    def __init__(self):
        self.current = {}
        self.applied = []

    def get(self, plan_id, symbol):
        return self.current.get((plan_id, symbol))

    def apply(self, *, input_event_id, decision, outbox_event):
        self.current[(decision.plan_id, decision.symbol)] = decision
        self.applied.append((input_event_id, decision, outbox_event))
        return True

    def close(self):
        return None


def monitor_snapshot():
    return {
        "collected_at": "2026-09-24T09:45:00+08:00",
        "plan_date": "2026-09-24",
        "stocks": [
            {
                "code": "002635",
                "plan": {"previous_close": 16.79},
                "quote": {
                    "quote_time": "2026-09-24T09:45:00+08:00",
                    "price": 17.5,
                    "open": 17.1,
                    "high": 17.6,
                    "low": 17.0,
                    "previous_close": 16.79,
                    "amount": 500000000,
                    "volume": 1234,
                    "bid": 17.49,
                    "ask": 17.5,
                    "bid_volume": 100,
                    "ask_volume": 50,
                    "candles": [
                        {
                            "time": "2026-09-24T09:44:00+08:00",
                            "open": 17.4,
                            "high": 17.6,
                            "low": 17.3,
                            "close": 17.5,
                            "price": 17.5,
                            "volume": 100,
                            "amount": 1750,
                        }
                    ],
                },
                "advice": {
                    "state": "watch",
                    "label": "观察",
                    "reason": "等待封板",
                    "tone": "neutral",
                },
            }
        ],
    }


class AsyncPersistenceTest(unittest.TestCase):
    def writer(self, market_store):
        cache = FakeSnapshotCache()
        decisions = FakeDecisionRepository()
        writer = AsyncMarketPersistence(
            market_store=market_store,
            snapshot_cache=cache,
            decision_repository=decisions,
            report_identity=ReportIdentity(
                run_id="run",
                strategy_version_id="version",
                plan_id="plan",
            ),
            snapshot_ttl_seconds=90,
            queue_size=4,
            batch_size=4,
        )
        return writer, cache, decisions

    def test_batch_deduplicates_minute_bar_and_persists_projection(self):
        market = FakeMarketStore()
        writer, cache, decisions = self.writer(market)
        writer.submit(monitor_snapshot())
        writer.submit(monitor_snapshot())
        writer.close()

        self.assertEqual(len(market.quotes), 2)
        self.assertEqual(len(market.bars), 1)
        self.assertEqual(len(decisions.applied), 1)
        self.assertEqual(cache.calls[-1][0], "2026-09-24")
        self.assertEqual(cache.calls[-1][2], 90)
        self.assertIsNone(writer.status()["last_error"])

    def test_clickhouse_failure_is_visible_but_other_targets_continue(self):
        market = FakeMarketStore(fail=True)
        writer, cache, decisions = self.writer(market)
        writer.submit(monitor_snapshot())
        writer.close()

        status = writer.status()
        self.assertEqual(status["failure_count"], 1)
        self.assertIn("clickhouse unavailable", status["last_error"])
        self.assertEqual(len(decisions.applied), 1)
        self.assertEqual(len(cache.calls), 1)

    def test_postgres_failure_prevents_uncommitted_redis_projection(self):
        market = FakeMarketStore()
        cache = FakeSnapshotCache()
        decisions = FakeDecisionRepository()

        def unavailable(_plan_id, _symbol):
            raise RuntimeError("postgres unavailable")

        decisions.get = unavailable
        writer = AsyncMarketPersistence(
            market_store=market,
            snapshot_cache=cache,
            decision_repository=decisions,
            report_identity=ReportIdentity(
                run_id="run",
                strategy_version_id="version",
                plan_id="plan",
            ),
        )
        writer.submit(monitor_snapshot())
        writer.close()

        self.assertIn("postgres unavailable", writer.status()["last_error"])
        self.assertEqual(cache.calls, [])
        self.assertEqual(len(market.quotes), 1)


class StorageSettingsTest(unittest.TestCase):
    def test_storage_is_off_by_default_and_validates_mode(self):
        self.assertFalse(StorageSettings.from_env({}).enabled)
        settings = StorageSettings.from_env(
            {
                "BANXIA_STORAGE_MODE": "required",
                "BANXIA_WRITER_BATCH_SIZE": "8",
                "BANXIA_MINIO_SECURE": "true",
            }
        )
        self.assertTrue(settings.required)
        self.assertEqual(settings.writer_batch_size, 8)
        self.assertTrue(settings.minio_secure)
        with self.assertRaises(ValueError):
            StorageSettings.from_env({"BANXIA_STORAGE_MODE": "silent"})


class FakeReportObjectStore:
    def __init__(self, fail_format=None):
        self.fail_format = fail_format
        self.closed = False

    def put(self, *, object_key, content, content_type, metadata):
        if metadata["format"] == self.fail_format:
            raise RuntimeError("upload failed")
        return ReportAsset(
            report_id=metadata["report_id"],
            format=metadata["format"],
            object_key=object_key,
            content_hash=hashlib.sha256(content).hexdigest(),
            content_type=content_type,
            size_bytes=len(content),
        )

    def close(self):
        self.closed = True


class FakeReportRepository:
    def __init__(self):
        self.calls = []
        self.closed = False

    def persist_report(self, report, **kwargs):
        self.calls.append((report, kwargs))
        return ReportIdentity(
            run_id="run",
            strategy_version_id="version",
            plan_id="plan",
        )

    def close(self):
        self.closed = True


class ReportDualWriteTest(unittest.TestCase):
    def paths(self, root):
        result = {}
        for name, filename in (
            ("json", "candidates.json"),
            ("csv", "candidates.csv"),
            ("markdown", "report.md"),
        ):
            path = Path(root) / filename
            path.write_text(name, encoding="utf-8")
            result[name] = path
        return result

    def report(self):
        return {
            "as_of": "2026-09-24",
            "generated_at": "2026-09-24T16:20:00+08:00",
        }

    def test_best_effort_records_partial_upload_and_still_writes_postgres(self):
        object_store = FakeReportObjectStore(fail_format="csv")
        repository = FakeReportRepository()
        settings = StorageSettings(mode="best_effort")
        with tempfile.TemporaryDirectory() as directory, patch(
            "banxia_strategy.application.persistence._create_minio",
            return_value=object_store,
        ), patch(
            "banxia_strategy.application.persistence._create_postgres",
            return_value=repository,
        ):
            result = persist_report_copy(
                self.report(),
                self.paths(directory),
                strategy_config={},
                settings=settings,
            )

        self.assertEqual(len(result.assets), 2)
        self.assertIn("minio:csv: upload failed", result.errors)
        self.assertEqual(len(repository.calls), 1)
        self.assertEqual(len(repository.calls[0][1]["assets"]), 2)
        self.assertTrue(object_store.closed)
        self.assertTrue(repository.closed)

    def test_required_mode_raises_after_attempting_all_targets(self):
        object_store = FakeReportObjectStore(fail_format="csv")
        repository = FakeReportRepository()
        settings = StorageSettings(mode="required")
        with tempfile.TemporaryDirectory() as directory, patch(
            "banxia_strategy.application.persistence._create_minio",
            return_value=object_store,
        ), patch(
            "banxia_strategy.application.persistence._create_postgres",
            return_value=repository,
        ):
            with self.assertRaisesRegex(RuntimeError, "minio:csv"):
                persist_report_copy(
                    self.report(),
                    self.paths(directory),
                    strategy_config={},
                    settings=settings,
                )
        self.assertEqual(len(repository.calls), 1)


if __name__ == "__main__":
    unittest.main()
