from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

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

    def set(self, key, value, ex):
        self.values[key] = value
        self.calls.append((key, value, ex))

    def get(self, key):
        return self.values.get(key)


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
            decision=object(),
            outbox_event=decision_event,
        )
        self.assertFalse(result)
        self.assertEqual(len(cursor.calls), 1)
        self.assertIn("ON CONFLICT DO NOTHING", cursor.calls[0][0])

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
        self.assertEqual(len(statements), 5)
        self.assertIn("INSERT INTO banxia.decision_state", statements[2])
        self.assertIn("INSERT INTO banxia.decision_event", statements[3])
        self.assertIn("INSERT INTO banxia.outbox_event", statements[4])
        self.assertEqual(cursor.calls[2][1][2], "watch")
        self.assertEqual(cursor.calls[4][1][1], "strategy.decision.v1")


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
