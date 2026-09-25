from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from banxia_strategy.execution_analysis import (
    _daily_bar_from_minutes,
    aggregate_periods,
    classify_candidate,
    load_daily_snapshot,
    summarize_stocks,
    write_reports,
)


def candidate():
    return {
        "code": "600001",
        "name": "测试股份",
        "industry": "测试题材",
        "rank": 1,
        "score": 70,
        "latest_price": 10.0,
        "plan": {
            "open_min_pct": 0.5,
            "open_max_pct": 5.0,
            "entry_cutoff_time": "10:00",
            "manual_max_intraday_breaks": 1,
            "reject_below_previous_close": True,
        },
    }


def bar(close=11.0):
    return {"open": 10.2, "high": 11.0, "low": 10.0, "close": close}


def minutes(prices=None):
    values = [10.2] * 240
    for index, value in (prices or {}).items():
        values[index] = value
    return {"prices": values, "volumes": [100] * 240}


class ExecutionClassificationTest(unittest.TestCase):
    def test_daily_bar_can_be_reconstructed_from_complete_minutes(self):
        data = minutes({0: 10.1, 5: 11.0, 239: 10.8})
        self.assertEqual(
            _daily_bar_from_minutes(data),
            {"open": 10.1, "high": 11.0, "low": 10.1, "close": 10.8},
        )

    def test_sealed_limit_without_post_touch_open_is_not_proven_buyable(self):
        data = minutes({index: 11.0 for index in range(5, 240)})
        result = classify_candidate(candidate(), "2026-09-24", bar(), data)
        self.assertFalse(result["buyable"])
        self.assertFalse(result["success"])
        self.assertEqual(result["reason_code"], "no_proven_fill_window")

    def test_touch_open_and_reseal_before_cutoff_is_success(self):
        data = minutes({5: 11.0, 6: 10.9, 7: 11.0})
        result = classify_candidate(candidate(), "2026-09-24", bar(), data)
        self.assertTrue(result["buyable"])
        self.assertTrue(result["success"])
        self.assertEqual(result["first_touch_time"], "09:36:00")
        self.assertEqual(result["buy_window_time"], "09:37:00")
        self.assertEqual(result["confirmation_time"], "09:38:00")
        self.assertEqual(result["break_count_before_cutoff"], 1)

    def test_breach_before_touch_permanently_rejects_candidate(self):
        data = minutes({2: 9.99, 5: 11.0, 6: 10.9, 7: 11.0})
        result = classify_candidate(candidate(), "2026-09-24", bar(), data)
        self.assertFalse(result["buyable"])
        self.assertEqual(result["reason_code"], "breached_reference")

    def test_touch_at_ten_oclock_is_outside_entry_window(self):
        data = minutes({29: 11.0, 30: 10.9, 31: 11.0})
        result = classify_candidate(candidate(), "2026-09-24", bar(), data)
        self.assertFalse(result["buyable"])
        self.assertEqual(result["reason_code"], "no_early_touch")

    def test_buyable_stock_that_does_not_close_limit_is_failure(self):
        data = minutes({5: 11.0, 6: 10.9, 7: 11.0})
        result = classify_candidate(candidate(), "2026-09-24", bar(close=10.8), data)
        self.assertTrue(result["buyable"])
        self.assertFalse(result["success"])
        self.assertEqual(result["reason_code"], "buyable_but_failed")


class ExecutionAggregationTest(unittest.TestCase):
    def test_periods_use_verified_candidates_as_weighted_denominator(self):
        success = {"minute_complete": True, "buyable": True, "success": True}
        failure = {"minute_complete": True, "buyable": False, "success": False}
        missing = {"minute_complete": False, "buyable": False, "success": False}
        days = [
            {"trade_date": "2026-09-01", "stocks": [success]},
            {"trade_date": "2026-09-02", "stocks": [failure, failure, failure, missing]},
        ]
        summary = summarize_stocks([success, failure, failure, failure, missing])
        self.assertEqual(summary["success_rate_pct"], 25)
        self.assertEqual(summary["buyable_close_rate_pct"], 100)
        week = aggregate_periods(days, "week")[0]
        self.assertEqual(week["verified_count"], 4)
        self.assertEqual(week["success_rate_pct"], 25)
        self.assertEqual(len(aggregate_periods(days, "month")), 1)
        self.assertEqual(len(aggregate_periods(days, "year")), 1)

    def test_execution_success_depends_on_buyability_not_closing_board(self):
        buyable_without_close = {
            "minute_complete": True,
            "buyable": True,
            "success": False,
        }
        summary = summarize_stocks([buyable_without_close])
        self.assertEqual(summary["execution_success_count"], 1)
        self.assertEqual(summary["execution_success_rate_pct"], 100)
        self.assertEqual(summary["success_count"], 0)
        self.assertEqual(summary["buyable_close_rate_pct"], 0)

    def test_multiple_snapshots_are_reduced_to_required_daily_bars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(
                '{"histories":{"600001":[{"datetime":"2025-01-02","close":10},'
                '{"datetime":"2025-01-03","close":11}],"600002":[{"datetime":"2025-01-02"}]}}'
            )
            second.write_text(
                '{"histories":{"600001":[{"datetime":"2025-01-03","close":99},'
                '{"datetime":"2025-01-06","close":12}]}}'
            )
            snapshot = load_daily_snapshot(
                [first, second],
                [("2025-01-02", "600001"), ("2025-01-06", "600001")],
            )
        self.assertEqual(
            [row["close"] for row in snapshot["histories"]["600001"]],
            [10, 12],
        )
        self.assertNotIn("600002", snapshot["histories"])

    def test_csv_export_ignores_internal_fields_not_declared_as_columns(self):
        stock = {
            "trade_date": "2026-09-24",
            "symbol": "600001",
            "name": "测试股份",
            "buyable": False,
            "success": False,
            "open": 10.2,
            "high": 11.0,
            "low": 10.0,
            "close": 11.0,
            "minute_complete": True,
            "internal_future_field": "must not break csv",
        }
        strategy = {
            "strategy_name": "测试策略",
            "summary": summarize_stocks([stock]),
            "periods": {
                frequency: aggregate_periods(
                    [{"trade_date": "2026-09-24", "stocks": [stock]}],
                    frequency,
                )
                for frequency in ("day", "week", "month", "year")
            },
            "days": [{"trade_date": "2026-09-24", "stocks": [stock]}],
        }
        result = {
            "start": "2026-09-24",
            "end": "2026-09-24",
            "strategies": [strategy],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_reports(result, output)
            with (output / "stocks.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["strategy_name"], "测试策略")
        self.assertEqual(rows[0]["symbol"], "600001")
        self.assertNotIn("internal_future_field", rows[0])


if __name__ == "__main__":
    unittest.main()
