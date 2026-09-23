from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from banxia_strategy.strategy import StrategyConfig, StrategyEngine, write_report


def row(
    code,
    name,
    industry,
    board=1,
    first="09:42:00",
    last="09:45:00",
    breaks=0,
    amount=600_000_000,
    turnover=10.0,
    float_cap=8_000_000_000,
    seal=60_000_000,
):
    return {
        "代码": code,
        "名称": name,
        "所属行业": industry,
        "连板数": board,
        "首次封板时间": first,
        "最后封板时间": last,
        "炸板次数": breaks,
        "成交额": amount,
        "换手率": turnover,
        "流通市值": float_cap,
        "封板资金": seal,
        "最新价": 12.34,
    }


class FakeProvider:
    def __init__(self):
        self.sessions = [
            date(2026, 9, 21),
            date(2026, 9, 22),
            date(2026, 9, 23),
        ]
        self.pools = {
            self.sessions[0]: [
                row("600010", "旧龙头", "人工智能", board=2),
                row("600020", "消费样本", "消费", board=1),
            ],
            self.sessions[1]: [
                row("600010", "旧龙头", "人工智能", board=3),
                row("600021", "AI前排", "人工智能", board=1),
            ],
            self.sessions[2]: [
                row("600010", "旧龙头", "人工智能", board=4),
                row("600001", "优质首板", "人工智能", first="09:36:00"),
                row(
                    "600002",
                    "次优首板",
                    "人工智能",
                    first="13:50:00",
                    breaks=2,
                    seal=15_000_000,
                ),
                row("300001", "创业样本", "人工智能"),
                row("600003", "ST风险", "消费"),
            ],
        }

    def trading_dates(self):
        return self.sessions

    def limit_up_pool(self, session):
        return self.pools.get(session, [])

    def broken_board_pool(self, session):
        return [{"代码": "600099"}]


class StrategyEngineTest(unittest.TestCase):
    def setUp(self):
        self.config = StrategyConfig(
            lookback_sessions=3,
            max_candidates=3,
            max_per_industry=1,
            minimum_score=45,
            position_limit_pct=20,
            portfolio_risk_limit_pct=40,
        )

    def test_filters_and_ranks_candidates(self):
        report = StrategyEngine(FakeProvider(), self.config).run(date(2026, 9, 23))
        self.assertEqual(report.as_of, "2026-09-23")
        self.assertIsNone(report.next_session)
        self.assertEqual([item.code for item in report.candidates], ["600001"])
        candidate = report.candidates[0]
        self.assertEqual(candidate.rank, 1)
        self.assertEqual(candidate.strategy, "龙头补涨")
        self.assertEqual(candidate.industry_max_board, 4)
        self.assertGreater(candidate.score, 60)
        self.assertNotIn("300001", [item.code for item in report.candidates])
        self.assertNotIn("600003", [item.code for item in report.candidates])

    def test_report_files_are_consistent(self):
        report = StrategyEngine(FakeProvider(), self.config).run(date(2026, 9, 23))
        with tempfile.TemporaryDirectory() as directory:
            paths = write_report(report, Path(directory))
            payload = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertEqual(payload["candidates"][0]["code"], "600001")
            with paths["csv"].open(encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["code"], "600001")
            markdown = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("优质首板", markdown)
            self.assertIn("不构成投资建议", markdown)

    def test_missing_broken_board_data_is_marked(self):
        provider = FakeProvider()

        def unavailable(_session):
            raise RuntimeError("upstream unavailable")

        provider.broken_board_pool = unavailable
        report = StrategyEngine(provider, self.config).run(date(2026, 9, 23))
        self.assertFalse(report.market["broken_board_data_available"])
        self.assertIsNone(report.market["break_rate_pct"])


if __name__ == "__main__":
    unittest.main()
