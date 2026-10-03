from __future__ import annotations

import unittest
from typing import Optional

from banxia_strategy.monthly_target_research import (
    portfolio_summary,
    research_rules,
)


def trade(
    *,
    symbol: str,
    d2_date: str,
    d3_date: str,
    change: float,
    result: Optional[float],
):
    return {
        "symbol": symbol,
        "name": symbol,
        "board": "main",
        "d2_date": d2_date,
        "d3_date": d3_date,
        "entry_mode": "early",
        "buy_status": "filled_proxy",
        "d1_change_pct": change,
        "d1_amount_cny": 800_000_000,
        "d1_close_location": 0.95,
        "d1_return_5d_pct": 12.0,
        "d1_composite_score": change,
        "d2_open_gap_pct": 3.0,
        "d2_first_minute_change_pct": 4.0,
        "early_net_return_pct": result,
        "protect_d1_close": True,
    }


class MonthlyTargetResearchTest(unittest.TestCase):
    def setUp(self):
        self.rule = {
            "minimum_d1_change_pct": 5.0,
            "minimum_d1_amount_cny": 500_000_000,
            "minimum_d1_close_location": 0.9,
            "maximum_d1_return_5d_pct": 20.0,
            "d2_open_gap_pct": [1.0, 5.0],
            "minimum_d2_first_minute_change_pct": 3.0,
            "entry_mode": "early",
            "protect_d1_close": True,
            "exit_mode": "early",
            "maximum_daily_entries": 1,
            "rank_by": "change",
        }

    def test_months_without_a_trade_are_target_failures(self):
        result = portfolio_summary(
            [
                trade(
                    symbol="000001",
                    d2_date="2025-01-02",
                    d3_date="2025-01-03",
                    change=9.0,
                    result=30.0,
                )
            ],
            self.rule,
            ["2025-01", "2025-02"],
        )

        self.assertEqual(result["hit_count"], 1)
        self.assertEqual(result["hit_rate"], 0.5)
        self.assertAlmostEqual(result["monthly_returns_pct"][0], 6.0)
        self.assertEqual(result["monthly_returns_pct"][1], 0.0)

    def test_daily_limit_uses_only_pre_entry_ranking_fields(self):
        result = portfolio_summary(
            [
                trade(
                    symbol="000001",
                    d2_date="2025-01-02",
                    d3_date="2025-01-03",
                    change=9.0,
                    result=-5.0,
                ),
                trade(
                    symbol="000002",
                    d2_date="2025-01-02",
                    d3_date="2025-01-03",
                    change=8.0,
                    result=30.0,
                ),
            ],
            self.rule,
            ["2025-01"],
        )

        self.assertEqual(result["trade_count"], 1)
        self.assertAlmostEqual(result["monthly_returns_pct"][0], -1.0)
        self.assertEqual(result["_trades"][0]["symbol"], "000001")

    def test_unknown_exit_is_penalized_instead_of_dropped(self):
        result = portfolio_summary(
            [
                trade(
                    symbol="000001",
                    d2_date="2025-01-02",
                    d3_date="2025-01-03",
                    change=9.0,
                    result=None,
                )
            ],
            self.rule,
            ["2025-01"],
        )

        self.assertAlmostEqual(result["monthly_returns_pct"][0], -2.0)

    def test_search_space_keeps_monthly_portfolio_choices_explicit(self):
        rules = list(research_rules())

        self.assertEqual(len(rules), 46_656)
        self.assertEqual(
            {rule["maximum_daily_entries"] for rule in rules},
            {1, 3, 5},
        )
        self.assertEqual(
            {rule["exit_mode"] for rule in rules},
            {"early", "target_3_0", "target_5_1", "target_7_0"},
        )


if __name__ == "__main__":
    unittest.main()
