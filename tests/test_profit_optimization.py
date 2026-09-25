from __future__ import annotations

import unittest

from banxia_strategy.profit_optimization import (
    chronological_split,
    select_before_holdout,
    summarize_returns,
    trial_specs,
)
from banxia_strategy.strategy_config import StrategyConfig


def period(count, mean, *, unsellable=0):
    return {
        "trade_count": count,
        "unsellable_proxy_count": unsellable,
        "mean_net_return_pct": mean,
    }


class ProfitOptimizationTest(unittest.TestCase):
    def test_chronological_split_keeps_holdout_last(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 21)]
        split = chronological_split(reversed(dates))
        self.assertEqual(len(split["train"]), 12)
        self.assertEqual(len(split["validation"]), 4)
        self.assertEqual(len(split["holdout"]), 4)
        self.assertLess(split["train"][-1], split["validation"][0])
        self.assertLess(split["validation"][-1], split["holdout"][0])

    def test_return_summary_ignores_unpriced_trade_but_exposes_it(self):
        summary = summarize_returns([
            {"net_return_pct": 2.0},
            {"net_return_pct": -1.0},
            {"net_return_pct": None},
        ])
        self.assertEqual(summary["trade_count"], 3)
        self.assertEqual(summary["priced_trade_count"], 2)
        self.assertEqual(summary["unsellable_proxy_count"], 1)
        self.assertEqual(summary["mean_net_return_pct"], 0.5)

    def test_selection_uses_only_train_and_validation(self):
        baseline = {
            "spec": {"minimum_score": 58.0, "entry_cutoff_time": "10:00"},
            "train": period(6, 1.0),
            "validation": period(2, 2.0),
        }
        selected = {
            "spec": {"minimum_score": 58.0, "entry_cutoff_time": "09:45"},
            "train": period(6, 2.0),
            "validation": period(2, 3.0),
        }
        rejected = {
            "spec": {"minimum_score": 62.0, "entry_cutoff_time": "09:45"},
            "train": period(5, 5.0),
            "validation": period(1, 20.0),
        }
        self.assertIs(select_before_holdout([baseline, selected, rejected], baseline), selected)

        selected["holdout"] = period(1, -99.0)
        self.assertIs(select_before_holdout([baseline, selected, rejected], baseline), selected)

    def test_search_space_only_tightens_minimum_score(self):
        base = StrategyConfig()
        specs = trial_specs(base)
        self.assertTrue(all(item.minimum_score >= base.minimum_score for item in specs))
        self.assertIn("09:45", {item.entry_cutoff_time for item in specs})
        self.assertIn(base.entry_cutoff_time, {item.entry_cutoff_time for item in specs})


if __name__ == "__main__":
    unittest.main()
