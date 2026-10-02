from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, asdict
from datetime import date

from banxia_strategy.strategy import StrategyEngine
from banxia_strategy.strategy_archetypes import (
    INITIAL_STRATEGY_CODES,
    STRATEGY_ARCHETYPES,
    accepts_candidate,
    initial_strategy_config,
)
from banxia_strategy.strategy_config import ConfigError, StrategyConfig


class StrategyArchetypeRegistryTest(unittest.TestCase):
    def test_registry_contains_ten_unique_protected_initial_strategies(self):
        self.assertEqual(len(STRATEGY_ARCHETYPES), 10)
        self.assertEqual(len({item.key for item in STRATEGY_ARCHETYPES}), 10)
        self.assertEqual(len({item.code for item in STRATEGY_ARCHETYPES}), 10)
        self.assertEqual(
            INITIAL_STRATEGY_CODES,
            frozenset(item.code for item in STRATEGY_ARCHETYPES),
        )
        self.assertTrue(all(item.code.startswith("banxia-") for item in STRATEGY_ARCHETYPES))

    def test_each_archetype_builds_a_valid_config_and_cannot_be_changed_to_unknown(self):
        defaults = asdict(StrategyConfig())
        for profile in STRATEGY_ARCHETYPES:
            with self.subTest(profile=profile.key):
                config = StrategyConfig.from_mapping(
                    initial_strategy_config(profile, defaults)
                )
                self.assertEqual(config.strategy_archetype, profile.key)
        with self.assertRaises(ConfigError):
            StrategyConfig.from_mapping({"strategy_archetype": "unknown"})
        with self.assertRaises(FrozenInstanceError):
            StrategyConfig().strategy_archetype = "capacity_trend"

    def test_first_board_strategy_uses_validated_quality_filters(self):
        profile = STRATEGY_ARCHETYPES[0]
        config = StrategyConfig.from_mapping(
            initial_strategy_config(profile, asdict(StrategyConfig()))
        )

        self.assertEqual(config.minimum_turnover_pct, 3.0)
        self.assertEqual(config.maximum_turnover_pct, 18.0)
        self.assertEqual(config.maximum_first_seal_time, "10:30")

    def test_representative_candidate_predicates_cover_all_archetypes(self):
        market = {"score": 40}
        industry = {"today_count": 3, "max_board": 4}
        rows = {
            "first_board_second_board": {"board_count": 1},
            "mainline_leader_relay": {"board_count": 3},
            "low_position_first_board": {
                "board_count": 1,
                "position_20d": 0.4,
                "return_20d_pct": 12,
            },
            "divergence_reseal": {"board_count": 1, "break_count": 1},
            "leader_first_yin": {
                "board_count": 0,
                "previous_board_count": 3,
                "is_bearish": True,
                "days_since_limit_up": 1,
            },
            "broken_board_reversal": {
                "board_count": 0,
                "days_since_limit_up": 2,
                "reversal_confirmed": True,
            },
            "theme_catchup": {"board_count": 1},
            "capacity_trend": {"board_count": 0, "trend_confirmed": True},
            "sentiment_repair": {
                "board_count": 0,
                "days_since_limit_up": 3,
                "repair_confirmed": True,
            },
            "auction_volume_breakout": {"board_count": 1},
        }
        for profile in STRATEGY_ARCHETYPES:
            with self.subTest(profile=profile.key):
                self.assertTrue(
                    accepts_candidate(
                        profile.key,
                        rows[profile.key],
                        industry,
                        market,
                    )
                )


class StrategyPoolDispatchTest(unittest.TestCase):
    def test_engine_dispatches_archetype_to_provider_strategy_pool(self):
        class Provider:
            source_name = "test"

            def __init__(self):
                self.requested = []

            def trading_dates(self):
                return [date(2026, 9, 23)]

            def limit_up_pool(self, session):
                return [{
                    "代码": "600001",
                    "名称": "题材样本",
                    "所属行业": "容量题材",
                    "连板数": 1,
                    "最新价": 10,
                    "成交额": 1_000_000_000,
                    "换手率": 10,
                    "流通市值": 10_000_000_000,
                    "封板资金": 50_000_000,
                }]

            def broken_board_pool(self, session):
                return []

            def strategy_pool(self, session, archetype):
                self.requested.append((session, archetype))
                return [{
                    "代码": "600002",
                    "名称": "趋势样本",
                    "所属行业": "容量题材",
                    "连板数": 0,
                    "最新价": 12,
                    "成交额": 1_200_000_000,
                    "换手率": 8,
                    "流通市值": 12_000_000_000,
                    "_trend_confirmed": True,
                    "_high": 12,
                    "_previous_high": 11.8,
                }]

        provider = Provider()
        profile = next(
            item for item in STRATEGY_ARCHETYPES
            if item.key == "capacity_trend"
        )
        raw = initial_strategy_config(profile, asdict(StrategyConfig()))
        raw["minimum_score"] = 0
        report = StrategyEngine(
            provider,
            StrategyConfig.from_mapping(raw),
        ).run(date(2026, 9, 23))

        self.assertEqual(provider.requested, [(date(2026, 9, 23), "capacity_trend")])
        self.assertEqual([item.code for item in report.candidates], ["600002"])
        self.assertEqual(report.candidates[0].plan["entry_mode"], "breakout")
        self.assertEqual(report.candidates[0].strategy, "容量趋势突破")


if __name__ == "__main__":
    unittest.main()
