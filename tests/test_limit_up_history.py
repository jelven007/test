from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from banxia_strategy.application.limit_up_history_sync import (
    LimitUpHistorySync,
)
from banxia_strategy.limit_up_history import (
    build_limit_up_history_rows,
    historical_capital,
    industry_from_f10,
    limit_price,
    reference_price,
)
from banxia_strategy.limit_up_collector import daily_bars


SHANGHAI = ZoneInfo("Asia/Shanghai")


def bar(day: int, close: float, *, open_price=None, high=None, low=None):
    return {
        "year": 2026,
        "month": 9,
        "day": day,
        "open": open_price if open_price is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "vol": 1_000_000,
        "amount": 1_100_000_000,
    }


class LimitUpHistoryCalculationTest(unittest.TestCase):
    def test_price_limit_uses_board_and_st_rules(self):
        session = date(2026, 9, 30)

        self.assertEqual(limit_price(10.01, "600001", "普通股份", session), 11.01)
        self.assertEqual(limit_price(10.01, "300001", "创业股份", session), 12.01)
        self.assertEqual(limit_price(10.01, "688001", "科创股份", session), 12.01)
        self.assertEqual(limit_price(10.01, "600002", "*ST样本", session), 10.51)

    def test_builds_metrics_and_marks_current_finance_estimate(self):
        histories = {
            "300001": [
                bar(22, 8.00),
                bar(23, 8.50),
                bar(24, 9.00),
                bar(25, 9.50),
                bar(28, 9.80),
                bar(29, 10.00),
                bar(
                    30,
                    12.00,
                    open_price=10.50,
                    high=12.00,
                    low=10.20,
                ),
            ]
        }
        securities = {
            "300001": {
                "symbol": "300001",
                "name": "样本科技",
                "exchange": "sz",
                "instrument_type": "stock",
                "board": "gem",
            }
        }
        collected_at = datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI)

        rows = build_limit_up_history_rows(
            histories=histories,
            securities=securities,
            industries={"300001": "电力设备-电池-锂电池"},
            finances={
                "300001": {
                    "zongguben": 200_000_000,
                    "liutongguben": 100_000_000,
                    "updated_date": 20260831,
                }
            },
            start_date=date(2026, 9, 30),
            end_date=date(2026, 9, 30),
            collected_at=collected_at,
        )

        self.assertEqual(len(rows), 1)
        item = rows[0]
        self.assertEqual(item["limit_price"], 12.0)
        self.assertEqual(item["total_market_cap_cny"], 2_400_000_000)
        self.assertEqual(item["float_market_cap_cny"], 1_200_000_000)
        self.assertEqual(item["turnover_pct"], 100.0)
        self.assertEqual(item["amplitude_pct"], 18.0)
        self.assertEqual(item["open_change_pct"], 5.0)
        self.assertEqual(item["change_pct"], 20.0)
        self.assertEqual(item["return_5d_pct"], 41.176471)
        self.assertEqual(item["industry"], "电力设备")
        self.assertEqual(item["raw"]["capital_basis"], "current_snapshot_estimate")
        self.assertEqual(item["capital_as_of_date"], date(2026, 8, 31))

    def test_gem_st_still_has_twenty_percent_limit(self):
        self.assertEqual(limit_price(10, "300001", "*ST样本", date(2026, 9, 30)), 12)

    def test_history_capital_uses_before_and_after_in_ten_thousand_shares(self):
        actions = [{
            "year": 2025, "month": 10, "day": 27, "category": 5,
            "qianzongguben": 30000, "panqianliutong": 20000,
            "houzongguben": 40000, "panhouliutong": 30000,
        }]
        before = historical_capital({}, actions, date(2025, 1, 1), date(2026, 10, 1))
        after = historical_capital({}, actions, date(2026, 1, 1), date(2026, 10, 1))
        self.assertEqual(before[:2], (300_000_000, 200_000_000))
        self.assertEqual(after[:2], (400_000_000, 300_000_000))
        self.assertEqual(after[3], "xdxr_capital_history")

    def test_dividend_reference_and_unhandled_split(self):
        self.assertEqual(reference_price(10, [{"category": 1, "fenhong": 1}]), 9.9)
        self.assertEqual(reference_price(10, [{"category": 1, "songzhuangu": 10}]), 5)
        self.assertIsNone(reference_price(10, [{"category": 11}]))

    def test_f10_industry_parser(self):
        self.assertEqual(industry_from_f10("│行业类别    ｜银行-股份制银行Ⅱ ｜"), "银行-股份制银行Ⅱ")
        self.assertIsNone(industry_from_f10("概念：人工智能"))

    def derive(self, bars, finance=None, actions=None):
        return build_limit_up_history_rows(
            histories={"600001": bars},
            securities={"600001": {"name": "样本", "market": "sh", "board": "main"}},
            finances={"600001": finance or {}}, actions={"600001": actions or []},
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
            collected_at=datetime(2026, 10, 1, tzinfo=SHANGHAI),
        )

    def test_above_limit_is_not_a_limit_up_and_five_percent_is_unverified(self):
        self.assertEqual(self.derive([bar(1, 10), bar(2, 11.1)]), [])
        rows = self.derive([bar(1, 10), bar(2, 10.5)])
        self.assertEqual(rows[0]["raw"]["limit_rule_basis"], "unverified_5pct_candidate")

    def test_first_five_ipo_sessions_excluded(self):
        bars = [bar(i + 1, value) for i, value in enumerate((10, 11, 12.1, 13.31, 14.64, 16.1))]
        rows = self.derive(bars, {"ipo_date": 20260901})
        self.assertEqual([row["trade_date"].day for row in rows], [6])

    def test_ex_dividend_limit_uses_adjusted_reference(self):
        rows = self.derive(
            [bar(1, 10), bar(2, 10.89)],
            actions=[{"year": 2026, "month": 9, "day": 2, "category": 1, "fenhong": 1}],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["previous_close"], 9.9)
        self.assertEqual(rows[0]["change_pct"], 10.0)


class FakeRepository:
    def __init__(self, latest=None):
        self.latest = latest
        self.calls = []

    def latest_limit_up_history_date(self):
        return self.latest

    def limit_up_history_lock(self):
        return nullcontext()

    def begin_limit_up_history_sync(self, start, end):
        self.calls.append(("begin", start, end))
        return "run-1"

    def complete_limit_up_history_sync(self, run_id, rows, **metadata):
        self.calls.append(("complete", run_id, rows, metadata))
        return len(rows)

    def fail_limit_up_history_sync(self, run_id, error):
        self.calls.append(("fail", run_id, str(error)))


class FakeProvider:
    def limit_up_history(self, start, end):
        return {
            "source": "mootdx",
            "start_date": start,
            "end_date": end,
            "collected_at": datetime(2026, 10, 1, tzinfo=SHANGHAI),
            "universe_count": 5000,
            "history_count": 4990,
            "missing_symbols": ["600999"],
            "successful_symbols": ["300001"],
            "coverage": [{"trade_date": end, "row_count": 1, "bar_count": 4990}],
            "rows": [{"symbol": "300001"}],
        }


class LimitUpHistorySyncTest(unittest.TestCase):
    def test_first_run_backfills_from_2025(self):
        repository = FakeRepository()
        result = LimitUpHistorySync(
            repository=repository,
            provider=FakeProvider(),
        ).run(end_date=date(2026, 9, 30))

        self.assertEqual(
            repository.calls[0],
            ("begin", date(2025, 1, 1), date(2026, 9, 30)),
        )
        self.assertEqual(result["row_count"], 1)

    def test_incremental_run_rechecks_recent_dates(self):
        repository = FakeRepository(latest=date(2026, 9, 30))
        LimitUpHistorySync(
            repository=repository,
            provider=FakeProvider(),
        ).run(end_date=date(2026, 10, 1))

        self.assertEqual(repository.calls[0][1], date(2026, 9, 16))

    def test_history_paging_keeps_five_prior_bars(self):
        import pandas as pd

        class Client:
            positions = []

            def bars(self, **kwargs):
                self.positions.append(kwargs["start"])
                end = date(2026, 10, 1) - timedelta(days=kwargs["start"])
                return pd.DataFrame([
                    {"datetime": str(end - timedelta(days=i)), "close": 10}
                    for i in range(800)
                ])

        client = Client()
        records = daily_bars(client, "600001", date(2024, 7, 26))
        self.assertEqual(client.positions, [0, 800])
        self.assertGreaterEqual(sum(date.fromisoformat(b["datetime"]) < date(2024, 7, 26) for b in records), 5)

    def test_pagination_detects_repeated_source_page(self):
        import pandas as pd

        class Client:
            def bars(self, **kwargs):
                return pd.DataFrame([
                    {"datetime": str(date(2026, 10, 1) - timedelta(days=i)), "close": 10}
                    for i in range(800)
                ])

        with self.assertRaisesRegex(RuntimeError, "no progress"):
            daily_bars(Client(), "600001", date(2020, 1, 1))


if __name__ == "__main__":
    unittest.main()
