from __future__ import annotations

import unittest
from datetime import date

from banxia_strategy.stock_history_backfill import (
    StockHistoryBackfill,
    normalize_daily_bars,
)


def bar(day: int, close: float) -> dict:
    return {
        "year": 2026,
        "month": 9,
        "day": day,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "vol": 1000,
        "amount": 10000,
    }


class Frame:
    def __init__(self, rows):
        self.rows = rows

    @property
    def empty(self):
        return not self.rows

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


class Client:
    def __init__(self, rows):
        self.rows = rows

    def bars(self, **_kwargs):
        return Frame(self.rows)

    def close(self):
        pass

    def finance(self, **_kwargs):
        return Frame([{"ipo_date": 0, "zongguben": 0, "liutongguben": 0}])

    def F10(self, **_kwargs):
        return "上市日期｜-｜"


class Provider:
    workers = 2
    servers = (("one", 1), ("two", 2))

    def securities(self):
        return [
            {"symbol": "600001", "name": "甲"},
            {"symbol": "000002", "name": "乙"},
        ]

    def _client(self, _server):
        return Client([bar(1, 10), bar(2, 10.5)])

    @staticmethod
    def _close(client):
        client.close()


class Store:
    def __init__(self):
        self.batches = []
        self.sync = []

    def completed_history_symbols(self, period):
        assert period == "day"
        return {"000002"}

    def upsert_history_bars_batch(self, bars, period):
        self.batches.append((bars, period))

    def mark_history_sync(self, symbol, period, **metadata):
        self.sync.append((symbol, period, metadata))


class StockHistoryBackfillTest(unittest.TestCase):
    def test_normalizes_range_and_preserves_raw_fields(self):
        rows = normalize_daily_bars(
            [bar(1, 10), bar(2, 10.5), bar(3, 0)],
            date(2026, 9, 2),
            date(2026, 9, 3),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-09-02")
        self.assertEqual(rows[0]["volume"], 1000)
        self.assertEqual(rows[0]["raw"]["close"], 10.5)

    def test_run_skips_completed_symbols_and_marks_success(self):
        store = Store()
        result = StockHistoryBackfill(
            provider=Provider(),
            store=store,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 2),
            batch_size=1,
        ).run()

        self.assertEqual(result.universe_count, 2)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.completed_count, 1)
        self.assertEqual(result.row_count, 2)
        self.assertEqual(result.failed_symbols, ())
        self.assertEqual(set(store.batches[0][0]), {"600001"})
        self.assertEqual(store.sync[0][0:2], ("600001", "day"))
        self.assertTrue(store.sync[0][2]["completed"])

    def test_unlisted_catalog_entries_are_excluded_without_completion(self):
        class UnlistedProvider(Provider):
            def securities(self):
                return [{"symbol": "301999", "name": "待上市"}]

            def _client(self, _server):
                return Client([])

        store = Store()
        result = StockHistoryBackfill(
            provider=UnlistedProvider(),
            store=store,
            start_date=date(2016, 1, 1),
            end_date=date(2026, 9, 30),
        ).run()

        self.assertEqual(result.excluded_symbols, ("301999",))
        self.assertEqual(result.failed_symbols, ())
        self.assertEqual(store.batches, [])
        self.assertEqual(store.sync, [])


if __name__ == "__main__":
    unittest.main()
