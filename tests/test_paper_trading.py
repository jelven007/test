from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from banxia_strategy.application.paper_trading import PaperTradingWorker
from banxia_strategy.storage_config import StorageSettings


def candidate():
    return {
        "code": "600001",
        "name": "模拟股份",
        "industry": "模拟题材",
        "latest_price": 10.0,
        "plan": {
            "entry_mode": "board_reseal",
            "open_min_pct": 0.5,
            "open_max_pct": 5.0,
            "minimum_first_minute_change_pct": 6.5,
            "entry_cutoff_time": "09:43",
            "manual_max_intraday_breaks": 1,
            "reject_below_previous_close": True,
        },
    }


class FakeProvider:
    def __init__(self):
        self.sessions = [
            date(2026, 10, 8),
            date(2026, 10, 9),
            date(2026, 10, 12),
        ]
        self.minutes = {}
        self.daily = {}

    def trading_dates(self):
        return self.sessions

    def historical_minutes(self, symbol, session):
        return self.minutes[(symbol, session)]

    def history_bars(self, symbol, _period, *, end_date, max_bars):
        del max_bars
        return [
            {
                "date": session.isoformat(),
                **bar,
            }
            for (stored_symbol, session), bar in self.daily.items()
            if stored_symbol == symbol and session <= end_date
        ]


class FakeRepository:
    def __init__(self):
        self.campaign = {
            "campaign_id": "campaign",
            "code": "first-board-positive-v1",
            "strategy_id": "strategy",
            "started_on": "2026-10-03",
            "target_sample_count": 30,
            "status": "running",
            "config_snapshot": {"next_day_take_profit_pct": 5.1},
        }
        self.entry_candidates = [
            {
                "plan_id": "plan",
                "reference_date": "2026-10-08",
                "entry_date": "2026-10-09",
                "candidate": candidate(),
            }
        ]
        self.trade = None

    def get_paper_campaign(self, _code):
        return self.campaign

    def ensure_paper_campaign(self, _code):
        return self.campaign

    def save_trading_sessions(self, _sessions):
        return None

    def get_strategy_day(self, _strategy_id, _trade_date):
        return {"next_plan": {"already_generated": True}}

    def list_paper_entry_candidates(self, _campaign_id, _through_date):
        return list(self.entry_candidates) if self.trade is None else []

    def save_paper_entry(self, _campaign_id, record):
        self.trade = {
            "paper_trade_id": "trade",
            **record,
        }

    def list_paper_pending_exits(self, _campaign_id, through_date):
        if (
            self.trade
            and self.trade["status"] in {"open", "exit_data_missing"}
            and date.fromisoformat(self.trade["exit_date"]) <= through_date
        ):
            return [self.trade]
        return []

    def save_paper_exit(self, _paper_trade_id, record):
        self.trade.update(record)

    def refresh_paper_campaign(self, _campaign_id):
        completed = int(
            bool(
                self.trade
                and self.trade["status"] in {"closed", "exit_unfilled"}
            )
        )
        positive = int(bool(completed and self.trade.get("positive")))
        return {
            "status": "running",
            "target_sample_count": 30,
            "completed_sample_count": completed,
            "remaining_sample_count": 30 - completed,
            "positive_count": positive,
            "positive_rate_pct": 100.0 if positive else None,
        }


def minute_bars(prices, volumes):
    return [
        {
            "close": price,
            "volume": volume,
        }
        for price, volume in zip(prices, volumes)
    ]


class PaperTradingWorkerTest(unittest.TestCase):
    def setUp(self):
        self.repository = FakeRepository()
        self.provider = FakeProvider()
        d2_prices = [10.7, 10.8, 11.0, 10.9] + [10.9] * 236
        d3_prices = [11.0] + [11.8] * 239
        volumes = [1_000.0] * 240
        self.provider.minutes[("600001", date(2026, 10, 9))] = (
            minute_bars(d2_prices, volumes)
        )
        self.provider.minutes[("600001", date(2026, 10, 12))] = (
            minute_bars(d3_prices, volumes)
        )
        self.provider.daily[("600001", date(2026, 10, 9))] = {
            "open": 10.2,
            "high": 11.0,
            "low": 10.2,
            "close": 10.9,
            "volume": 240_000,
        }
        self.provider.daily[("600001", date(2026, 10, 12))] = {
            "open": 11.0,
            "high": 11.8,
            "low": 11.0,
            "close": 11.8,
            "volume": 240_000,
        }
        self.worker = PaperTradingWorker(
            repository=self.repository,
            provider=self.provider,
            storage_settings=StorageSettings(),
            strategy_config_path=Path("config/strategy.json"),
            output_dir=Path("reports"),
        )

    def test_entry_and_next_session_exit_form_one_positive_sample(self):
        entry = self.worker.run(date(2026, 10, 9))

        self.assertEqual(entry.entries_processed, 1)
        self.assertEqual(self.repository.trade["status"], "open")
        self.assertEqual(self.repository.trade["entry_time"], "09:34:00")
        self.assertEqual(self.repository.trade["exit_date"], "2026-10-12")
        self.assertGreater(self.repository.trade["target_price"], 10.9)

        exit_result = self.worker.run(date(2026, 10, 12))

        self.assertEqual(exit_result.exits_processed, 1)
        self.assertEqual(self.repository.trade["status"], "closed")
        self.assertTrue(self.repository.trade["positive"])
        self.assertGreater(self.repository.trade["net_return_pct"], 5.1)
        self.assertEqual(exit_result.summary["completed_sample_count"], 1)

    def test_missing_entry_minutes_are_retryable_not_completed(self):
        self.provider.minutes[("600001", date(2026, 10, 9))] = []

        result = self.worker.run(date(2026, 10, 9))

        self.assertEqual(result.summary["completed_sample_count"], 0)
        self.assertEqual(self.repository.trade["status"], "entry_data_missing")
        self.assertIn("行情质量检查失败", self.repository.trade["rejection_reason"])


if __name__ == "__main__":
    unittest.main()
