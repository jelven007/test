from __future__ import annotations

import unittest
from datetime import date

from banxia_strategy.application.reference_sync import MarketReferenceSync
from banxia_strategy.mootdx_provider import REFERENCE_FILES
from banxia_strategy.ports.storage import ReportAsset


class FakeRepository:
    def __init__(self, *, trading_day=True):
        self.calls = []
        self.trading_day = trading_day

    def begin_market_reference_snapshot(self, as_of_date):
        self.calls.append(("begin", as_of_date))
        return "snapshot-1"

    def publish_market_reference_snapshot(self, snapshot_id, **kwargs):
        self.calls.append(("publish", snapshot_id, kwargs))
        return snapshot_id

    def fail_market_reference_snapshot(self, snapshot_id, error):
        self.calls.append(("fail", snapshot_id, str(error)))

    def is_trading_session(self, _as_of_date):
        return self.trading_day


class FakeObjectStore:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def put(self, *, object_key, content, content_type, metadata):
        self.calls.append((object_key, content, content_type, metadata))
        if self.fail:
            raise RuntimeError("object storage unavailable")
        return ReportAsset(
            report_id=metadata["report_id"],
            format=metadata["format"],
            object_key=object_key,
            content_hash="a" * 64,
            content_type=content_type,
            size_bytes=len(content),
        )


class FakeProvider:
    def __init__(self):
        self.quote_calls = []

    def reference_snapshot(self):
        return {
            "source_node": "example:7709",
            "raw_securities": [
                {"market": 1, "code": "600001", "name": "样本股份"}
            ],
            "expected_counts": {1: 1, 0: 0},
            "files": {
                filename: f"raw:{filename}".encode()
                for filename in REFERENCE_FILES
            },
            "securities": [
                {
                    "market": 1,
                    "exchange": "sh",
                    "instrument_type": "stock",
                    "symbol": "600001",
                    "name": "样本股份",
                    "board": "main",
                    "previous_close": 10,
                    "volume_unit": 100,
                    "decimal_point": 2,
                    "raw": {},
                }
            ],
            "memberships": [
                {
                    "block_type": "concept",
                    "source_code": "新能源车",
                    "block_name": "新能源车",
                    "symbol": "600001",
                    "exchange": "sh",
                    "source_filename": "block_gn.dat",
                    "raw": {},
                }
            ],
        }

    def quote_snapshots(self, symbols):
        self.quote_calls.append(tuple(symbols))
        return {
            "600001": {
                "code": "600001",
                "price": 10.5,
                "last_close": 10,
                "open": 10.1,
                "high": 10.8,
                "low": 9.9,
                "volume": 500000,
                "amount": 300000000,
                "servertime": "15:00:00",
            }
        }


class MarketReferenceSyncTest(unittest.TestCase):
    def test_raw_objects_are_written_before_database_publish(self):
        repository = FakeRepository()
        object_store = FakeObjectStore()
        provider = FakeProvider()
        worker = MarketReferenceSync(
            repository=repository,
            object_store=object_store,
            provider=provider,
        )

        result = worker.run(date(2026, 9, 25))

        self.assertEqual(repository.calls[0][0], "begin")
        self.assertEqual(repository.calls[-1][0], "publish")
        self.assertEqual(len(object_store.calls), len(REFERENCE_FILES) + 3)
        self.assertIn(
            "manifests/dataset=market_reference/",
            object_store.calls[-1][0],
        )
        publish = repository.calls[-1][2]
        self.assertEqual(publish["source_node"], "example:7709")
        self.assertEqual(publish["row_count"], 3)
        self.assertEqual(publish["expected_count"], 2)
        self.assertEqual(
            publish["daily_snapshots"][0]["data_state"],
            "available",
        )
        self.assertEqual(publish["daily_snapshots"][0]["change_pct"], 5.0)
        self.assertEqual(provider.quote_calls, [("600001",)])
        self.assertEqual(result["snapshot_id"], "snapshot-1")

    def test_non_trading_day_skips_daily_quote_collection(self):
        repository = FakeRepository(trading_day=False)
        object_store = FakeObjectStore()
        provider = FakeProvider()
        worker = MarketReferenceSync(
            repository=repository,
            object_store=object_store,
            provider=provider,
        )

        result = worker.run(date(2026, 9, 26))

        publish = repository.calls[-1][2]
        self.assertEqual(provider.quote_calls, [])
        self.assertEqual(publish["daily_snapshots"], [])
        self.assertEqual(result["daily_quote_count"], 0)
        self.assertEqual(result["missing_quote_count"], 0)
        self.assertEqual(len(object_store.calls), len(REFERENCE_FILES) + 2)

    def test_incomplete_daily_quotes_fail_before_publish(self):
        repository = FakeRepository()
        provider = FakeProvider()
        provider.quote_snapshots = lambda _symbols: {}
        worker = MarketReferenceSync(
            repository=repository,
            object_store=FakeObjectStore(),
            provider=provider,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "incomplete mootdx daily quotes",
        ):
            worker.run(date(2026, 9, 24))

        self.assertEqual(
            [call[0] for call in repository.calls],
            ["begin", "fail"],
        )

    def test_object_failure_marks_snapshot_failed_without_publish(self):
        repository = FakeRepository()
        worker = MarketReferenceSync(
            repository=repository,
            object_store=FakeObjectStore(fail=True),
            provider=FakeProvider(),
        )

        with self.assertRaisesRegex(RuntimeError, "object storage"):
            worker.run(date(2026, 9, 25))

        self.assertEqual(
            [call[0] for call in repository.calls],
            ["begin", "fail"],
        )


if __name__ == "__main__":
    unittest.main()
