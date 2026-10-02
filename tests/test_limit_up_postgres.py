from __future__ import annotations

import os
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from banxia_strategy.adapters.postgres import PostgresStorage
from banxia_strategy.limit_up_history import build_limit_up_history_rows


DSN = os.environ.get("BANXIA_TEST_POSTGRES_DSN")


@unittest.skipUnless(DSN, "requires isolated BANXIA_TEST_POSTGRES_DSN")
class LimitUpPostgresTest(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict

        if "test" not in conninfo_to_dict(DSN).get("dbname", "").lower():
            raise RuntimeError("test database required")
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("CREATE SCHEMA IF NOT EXISTS banxia")
            connection.execute(Path("migrations/postgres/016_limit_up_history.sql").read_text())
            connection.execute(Path("migrations/postgres/017_limit_up_exclusions.sql").read_text())
            connection.execute("TRUNCATE banxia.limit_up_history, banxia.limit_up_history_coverage, banxia.limit_up_history_sync")
        self.repository = PostgresStorage(dsn=DSN)
        self.day = date(2025, 1, 2)
        self.rows = build_limit_up_history_rows(
            histories={
                "600001": [
                    {"datetime": "2024-12-31", "close": 10, "open": 10, "high": 10, "low": 10},
                    {"datetime": "2025-01-02", "close": 11, "open": 10.2, "high": 11, "low": 10, "vol": 10000},
                ],
            },
            securities={"600001": {"name": "测试", "market": "sh", "board": "main"}},
            finances={"600001": {"zongguben": 200000000, "liutongguben": 100000000}},
            industries={"600001": "银行"},
            start_date=date(2025, 1, 1), end_date=self.day,
            collected_at=datetime(2026, 10, 2, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    def tearDown(self):
        self.repository.close()

    def save(self, rows=None, missing=(), symbols=("600001",), excluded=()):
        run_id = self.repository.begin_limit_up_history_sync(date(2025, 1, 1), self.day)
        self.repository.complete_limit_up_history_sync(
            run_id, self.rows if rows is None else rows, universe_count=2,
            history_count=len(symbols), missing_symbols=missing,
            successful_symbols=symbols, effective_end=self.day,
            excluded_symbols=excluded,
            coverage=[{"trade_date": self.day, "bar_count": len(symbols), "row_count": len(self.rows if rows is None else rows)}],
        )
        return run_id

    def read(self, **kwargs):
        return self.repository.list_limit_up_history(start_date=date(2025, 1, 1), end_date=self.day, **kwargs)

    def test_idempotence_filter_and_stale_fact_reconciliation(self):
        self.save()
        self.save()
        result = self.read(
            minimums={"total_market_cap_cny": 2e9, "turnover_pct": 1},
            maximums={"float_market_cap_cny": 1.2e9}, industry="银行",
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["capital_basis"], "current_snapshot_estimate")
        self.assertEqual(self.read(minimums={"change_pct": 11})["total"], 0)
        self.assertEqual(self.repository.latest_limit_up_history_date(), self.day)
        self.save(rows=[])
        self.assertEqual(self.read()["total"], 0)
        self.assertEqual(self.repository.limit_up_history_options()["covered_sessions"], 1)

    def test_partial_preserves_failed_symbols_and_does_not_advance_watermark(self):
        self.save(missing=("600002",))
        self.assertIsNone(self.repository.latest_limit_up_history_date())
        self.save(rows=[], missing=("600001",), symbols=("600002",))
        self.assertEqual(self.read()["total"], 1)
        options = self.repository.limit_up_history_options()
        self.assertEqual(options["latest_sync"]["status"], "partial")
        self.assertEqual(options["partial_sessions"], 1)

    def test_unverified_candidate_excluded_by_default(self):
        self.rows[0]["raw"]["limit_rule_basis"] = "unverified_5pct_candidate"
        self.save()
        self.assertEqual(self.read()["total"], 0)
        self.assertEqual(self.read(include_unverified=True)["total"], 1)

    def test_exclusion_evidence_is_persisted_without_blocking_watermark(self):
        excluded = [{"symbol": "301569", "reason": "not_yet_listed",
                     "evidence": {"ipo_date": 0, "f10_listing_date": "-"}}]
        self.save(excluded=excluded)
        options = self.repository.limit_up_history_options()
        self.assertEqual(options["latest_sync"]["excluded_symbols"], excluded)
        self.assertEqual(options["latest_sync"]["status"], "succeeded")
        self.assertEqual(options["partial_sessions"], 0)
        self.assertEqual(self.repository.latest_limit_up_history_date(), self.day)

    def test_lock_prevents_concurrent_collection(self):
        other = PostgresStorage(dsn=DSN)
        try:
            with self.repository.limit_up_history_lock():
                with self.assertRaisesRegex(RuntimeError, "正在运行"):
                    with other.limit_up_history_lock():
                        self.fail("second lock acquired")
            with other.limit_up_history_lock():
                pass
        finally:
            other.close()

    def test_failed_insert_rolls_back_deletion(self):
        self.save()
        broken = [{**self.rows[0], "close": -1}]
        with self.assertRaises(Exception):
            self.save(rows=broken)
        self.assertEqual(self.read()["total"], 1)
