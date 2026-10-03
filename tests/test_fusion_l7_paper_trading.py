from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from typing import Any

from banxia_strategy.application.fusion_l7 import (
    FusionL7CandidateScanner,
    FusionL7PaperTradingWorker,
    LAYER4_RULE_A_CLOSE_LOC_MAX,
    LAYER4_RULE_A_R_LAST30_MAX_PCT,
    LAYER7_STOP_LOSS_PCT,
    LAYER7_TAKE_PROFIT_PCT,
    simulate_layer7_exit,
)
from banxia_strategy.storage_config import StorageSettings


def _flat_minutes(prices: list[float], volumes: list[float]):
    return [
        {"close": p, "volume": v} for p, v in zip(prices, volumes)
    ]


def _rule_a_minutes(d1_close: float):
    prices = [d1_close + 0.3] * 210
    prices.extend([d1_close + 0.3 - 0.005 * (i + 1) for i in range(29)])
    prices.append(d1_close)
    volumes = [1000.0] * 240
    return _flat_minutes(prices, volumes)


def _sealed_minutes(d1_close: float):
    prices = [d1_close] * 240
    volumes = [1000.0] * 240
    return _flat_minutes(prices, volumes)


class FusionProvider:
    def __init__(self):
        self.sessions = [
            date(2026, 10, 8),
            date(2026, 10, 9),
            date(2026, 10, 12),
            date(2026, 10, 13),
        ]
        self.securities_list: list[dict[str, Any]] = []
        self.daily: dict[tuple[str, date], list[dict[str, Any]]] = {}
        self.minutes: dict[tuple[str, date], list[dict[str, Any]]] = {}

    def trading_dates(self):
        return self.sessions

    def securities(self):
        return [dict(item) for item in self.securities_list]

    def history_bars(self, symbol, _period, *, end_date, max_bars):
        del max_bars
        bars = self.daily.get(symbol, [])
        return [bar for bar in bars if date.fromisoformat(bar["date"]) <= end_date]

    def historical_minutes(self, symbol, session):
        return self.minutes.get((symbol, session), [])


class FusionRepository:
    def __init__(self):
        self.campaign = {
            "campaign_id": "fusion-campaign",
            "code": "fusion-l7-v1",
            "strategy_id": "paper-strategy",
            "started_on": "2026-10-03",
            "target_sample_count": 30,
            "status": "running",
            "config_snapshot": {},
        }
        self.candidates: list[dict[str, Any]] = []
        self.trades: dict[str, dict[str, Any]] = {}
        self._trade_sequence = 0

    def ensure_paper_campaign(self, _code):
        return self.campaign

    def get_paper_campaign(self, _code="fusion-l7-v1"):
        return self.campaign

    def save_trading_sessions(self, _sessions):
        return None

    def refresh_paper_campaign(self, _campaign_id):
        completed = sum(
            1
            for trade in self.trades.values()
            if trade["status"] in {"closed", "exit_unfilled"}
        )
        return {
            "status": "running",
            "target_sample_count": 30,
            "completed_sample_count": completed,
            "remaining_sample_count": 30 - completed,
            "positive_count": sum(
                1 for trade in self.trades.values() if trade.get("positive")
            ),
        }

    def upsert_fusion_l7_candidate(self, _campaign_id, record):
        for existing in self.candidates:
            if (
                existing["d1_date"] == record["d1_date"]
                and existing["symbol"] == record["symbol"]
            ):
                existing.update(record)
                return existing["candidate_id"]
        candidate_id = f"candidate-{len(self.candidates) + 1}"
        self.candidates.append(
            {"candidate_id": candidate_id, **record}
        )
        return candidate_id

    def list_fusion_l7_candidates(self, _campaign_id, d2_date):
        return [
            {**candidate}
            for candidate in self.candidates
            if candidate["d2_date"] == d2_date.isoformat()
            and not any(
                trade.get("fusion_candidate_id") == candidate["candidate_id"]
                and trade["status"] != "entry_data_missing"
                for trade in self.trades.values()
            )
        ]

    def save_paper_entry(self, _campaign_id, record):
        fusion_id = record.get("fusion_candidate_id")
        key = f"fusion:{fusion_id}:{record['symbol']}"
        existing = self.trades.get(key)
        if existing is not None and existing["status"] != "entry_data_missing":
            return
        self._trade_sequence += 1
        self.trades[key] = {
            "paper_trade_id": f"trade-{self._trade_sequence}",
            **record,
        }

    def list_paper_pending_exits(self, _campaign_id, through_date):
        return [
            trade
            for trade in self.trades.values()
            if trade["status"] in {"open", "exit_data_missing"}
            and date.fromisoformat(trade["exit_date"]) <= through_date
        ]

    def save_paper_exit(self, paper_trade_id, record):
        for trade in self.trades.values():
            if trade["paper_trade_id"] == paper_trade_id:
                trade.update(record)
                break


class Layer7ExitSimulationTest(unittest.TestCase):
    def test_take_profit_triggers_before_stop_loss(self):
        prices = [10.0, 10.3, 10.6]
        price, reason, index = simulate_layer7_exit(
            prices,
            d3_open=9.0,
            tp_pct=LAYER7_TAKE_PROFIT_PCT / 100.0,
            sl_pct=LAYER7_STOP_LOSS_PCT / 100.0,
        )
        self.assertEqual(reason, "layer7_take_profit")
        self.assertAlmostEqual(price, 10.5, places=4)
        self.assertEqual(index, 2)

    def test_stop_loss_wins_when_both_touched_same_minute(self):
        prices = [10.0, 9.7, 10.6]
        price, reason, _ = simulate_layer7_exit(
            prices,
            d3_open=9.0,
            tp_pct=LAYER7_TAKE_PROFIT_PCT / 100.0,
            sl_pct=LAYER7_STOP_LOSS_PCT / 100.0,
        )
        self.assertEqual(reason, "layer7_stop_loss")
        self.assertAlmostEqual(price, 9.75, places=4)

    def test_fallback_to_d3_open_when_neither_threshold_hit(self):
        prices = [10.0] + [10.05] * 239
        price, reason, index = simulate_layer7_exit(
            prices,
            d3_open=10.2,
            tp_pct=LAYER7_TAKE_PROFIT_PCT / 100.0,
            sl_pct=LAYER7_STOP_LOSS_PCT / 100.0,
        )
        self.assertEqual(reason, "layer7_d3_open_fallback")
        self.assertAlmostEqual(price, 10.2, places=4)
        self.assertIsNone(index)


class FusionL7ScannerTest(unittest.TestCase):
    def setUp(self):
        self.provider = FusionProvider()
        self.repository = FusionRepository()
        self.scanner = FusionL7CandidateScanner(
            repository=self.repository, provider=self.provider
        )
        self.provider.securities_list = [
            {"symbol": "600001", "name": "正常龙", "industry": "新能源"},
            {"symbol": "600002", "name": "*ST 弱", "industry": "退市"},
            {"symbol": "600003", "name": "封板王", "industry": "半导体"},
            {"symbol": "600004", "name": "弱势股", "industry": "医药"},
            {"symbol": "600005", "name": "基础涨", "industry": "化工"},
        ]
        self.provider.daily = {
            "600001": [
                {"date": "2026-10-07", "open": 9.9, "high": 10.0, "low": 9.8, "close": 10.0, "volume": 1_000_000},
                {"date": "2026-10-08", "open": 10.1, "high": 10.7, "low": 10.05, "close": 10.5, "volume": 2_000_000},
            ],
            "600002": [
                {"date": "2026-10-07", "open": 4.0, "high": 4.1, "low": 3.9, "close": 4.0, "volume": 500_000},
                {"date": "2026-10-08", "open": 4.0, "high": 4.3, "low": 3.9, "close": 4.3, "volume": 600_000},
            ],
            "600003": [
                {"date": "2026-10-07", "open": 18.0, "high": 18.2, "low": 17.8, "close": 18.0, "volume": 900_000},
                {"date": "2026-10-08", "open": 18.1, "high": 19.8, "low": 18.1, "close": 19.8, "volume": 1_200_000},
            ],
            "600004": [
                {"date": "2026-10-07", "open": 7.0, "high": 7.1, "low": 6.9, "close": 7.0, "volume": 600_000},
                {"date": "2026-10-08", "open": 7.0, "high": 7.1, "low": 6.95, "close": 7.07, "volume": 650_000},
            ],
            "600005": [
                {"date": "2026-10-07", "open": 20.0, "high": 20.1, "low": 19.9, "close": 20.0, "volume": 300_000},
                {"date": "2026-10-08", "open": 20.2, "high": 20.9, "low": 20.1, "close": 20.6, "volume": 500_000},
            ],
        }
        self.provider.minutes = {
            ("600001", date(2026, 10, 8)): _rule_a_minutes(10.5),
            ("600003", date(2026, 10, 8)): _sealed_minutes(19.8),
            ("600005", date(2026, 10, 8)): _flat_minutes(
                [20.5] * 240, [1000.0] * 240
            ),
        }

    def test_scanner_filters_st_sealed_and_weak_moves(self):
        kept = self.scanner.scan(
            self.repository.campaign["campaign_id"],
            d1_date=date(2026, 10, 8),
            d2_date=date(2026, 10, 9),
        )
        self.assertEqual(kept, 1)
        self.assertEqual(len(self.repository.candidates), 1)
        candidate = self.repository.candidates[0]
        self.assertEqual(candidate["symbol"], "600001")
        self.assertEqual(candidate["d2_date"], "2026-10-09")
        self.assertLess(
            candidate["r_last30_pct"],
            LAYER4_RULE_A_R_LAST30_MAX_PCT,
        )
        self.assertLess(
            candidate["close_location_day"],
            LAYER4_RULE_A_CLOSE_LOC_MAX,
        )


class FusionL7WorkerTest(unittest.TestCase):
    def setUp(self):
        self.provider = FusionProvider()
        self.repository = FusionRepository()
        self.repository.candidates.append(
            {
                "candidate_id": "candidate-1",
                "d1_date": "2026-10-08",
                "d2_date": "2026-10-09",
                "symbol": "600001",
                "name": "正常龙",
                "industry": "新能源",
                "d1_close": 10.5,
                "d1_change_pct": 5.0,
                "r_last30_pct": -0.8,
                "close_location_day": 0.4,
                "evidence": {},
                "lhb_check_enabled": False,
            }
        )
        self.repository.candidates.append(
            {
                "candidate_id": "candidate-2",
                "d1_date": "2026-10-08",
                "d2_date": "2026-10-09",
                "symbol": "600005",
                "name": "基础涨",
                "industry": "化工",
                "d1_close": 20.0,
                "d1_change_pct": 4.0,
                "r_last30_pct": -0.7,
                "close_location_day": 0.3,
                "evidence": {},
                "lhb_check_enabled": False,
            }
        )
        d2_prices_tp = [10.6, 10.7, 11.1, 11.3] + [11.0] * 236
        d3_prices = [11.0] * 240
        volumes = [500_000.0] * 240
        self.provider.minutes[("600001", date(2026, 10, 9))] = _flat_minutes(
            d2_prices_tp, volumes
        )
        self.provider.minutes[("600001", date(2026, 10, 12))] = _flat_minutes(
            d3_prices, volumes
        )
        d2_prices_gap_out = [21.4] + [21.4] * 239
        self.provider.minutes[("600005", date(2026, 10, 9))] = _flat_minutes(
            d2_prices_gap_out, volumes
        )
        self.provider.daily["600001"] = [
            {"date": "2026-10-08", "open": 10.1, "high": 10.7, "low": 10.05, "close": 10.5, "volume": 2_000_000},
            {"date": "2026-10-09", "open": 10.6, "high": 11.3, "low": 10.6, "close": 11.0, "volume": 2_000_000},
        ]
        self.provider.daily["600005"] = [
            {"date": "2026-10-08", "open": 20.2, "high": 20.9, "low": 20.1, "close": 20.0, "volume": 500_000},
            {"date": "2026-10-09", "open": 21.4, "high": 21.5, "low": 21.3, "close": 21.4, "volume": 300_000},
        ]
        self.worker = FusionL7PaperTradingWorker(
            repository=self.repository,
            provider=self.provider,
            storage_settings=StorageSettings(),
            strategy_config_path=Path("config/strategy.json"),
            output_dir=Path("reports"),
        )

    def test_entry_gates_on_layer6_gap_and_exit_hits_take_profit(self):
        entry = self.worker.run(date(2026, 10, 9))
        self.assertEqual(entry.entries_processed, 2)

        rejected = next(
            trade
            for trade in self.repository.trades.values()
            if trade["symbol"] == "600005"
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("Layer 6", rejected["rejection_reason"])

        open_trade = next(
            trade
            for trade in self.repository.trades.values()
            if trade["symbol"] == "600001"
        )
        self.assertEqual(open_trade["status"], "open")
        self.assertEqual(open_trade["entry_time"], "09:31:00")
        self.assertEqual(open_trade["exit_date"], "2026-10-12")

        exit_result = self.worker.run(date(2026, 10, 12))
        self.assertEqual(exit_result.exits_processed, 1)
        closed = next(
            trade
            for trade in self.repository.trades.values()
            if trade["symbol"] == "600001"
        )
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["exit_reason"], "layer7_take_profit")
        self.assertTrue(closed["positive"])


if __name__ == "__main__":
    unittest.main()
