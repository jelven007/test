from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from banxia_strategy.research import (
    aggregate_days, enough_samples, experiment_specs, group_periods,
    label_candidate, optimize, summarize,
)
from banxia_strategy.research_data import DatedResearchProvider
from banxia_strategy.research_files import LocalResearchStore
from banxia_strategy.research_storage import save_research
from banxia_strategy.strategy_config import StrategyConfig


def candidate():
    return {
        "code": "600001", "name": "测试股份", "score": 70, "industry": "电子",
        "latest_price": 10, "plan": {
            "open_min_pct": .5, "open_max_pct": 5, "entry_cutoff_time": "10:00",
        },
    }


def outcome(hit=True, auction=True):
    return {
        "status": "observed", "closed_limit_up": hit,
        "touched_limit_up": True, "auction_qualified": auction,
    }


class ResearchLabelsTest(unittest.TestCase):
    def test_touch_does_not_count_as_close_or_trade(self):
        snapshot = {"requested_end": "2026-09-24", "histories": {
            "600001": [{"datetime": "2026-09-24", "open": 10.5, "high": 11,
                        "low": 9.9, "close": 10.8, "vol": 100}],
        }}
        row = label_candidate(candidate(), "2026-09-24", snapshot)
        self.assertTrue(row["touched_limit_up"])
        self.assertFalse(row["closed_limit_up"])
        self.assertTrue(row["auction_qualified"])  # boundary +5%, float safe
        self.assertTrue(row["daily_below_reference"])
        self.assertEqual(row["entry_verification"], "unverified")
        self.assertIsNone(row["early_touch_proxy"])
        self.assertEqual(summarize([row])["accuracy_pct"], 0)
        self.assertIsNone(summarize([row])["execution_accuracy_pct"])

    def test_pending_missing_and_zero_volume_do_not_enter_denominator(self):
        snapshot = {"requested_end": "2026-09-24", "histories": {}}
        pending = label_candidate(candidate(), "2026-09-25", snapshot)
        missing = label_candidate(candidate(), "2026-09-24", snapshot)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(missing["status"], "missing")
        snapshot["histories"]["600001"] = [{
            "datetime": "2026-09-24", "open": 10.1, "high": 11,
            "low": 10, "close": 11, "vol": 0,
        }]
        self.assertEqual(label_candidate(candidate(), "2026-09-24", snapshot)["status"], "missing")
        summary = summarize([outcome(), pending, missing])
        self.assertEqual(summary["accuracy_pct"], 100)
        self.assertEqual(summary["observed_count"], 1)
        self.assertEqual(summary["pending_count"], 1)
        self.assertEqual(summary["missing_count"], 1)
        self.assertIsNone(summarize([pending, missing])["accuracy_pct"])
        self.assertIsNone(summarize([])["accuracy_pct"])

    def test_periods_use_weighted_counts_and_execution_date(self):
        days = []
        for stamp, rows in [
            ("2026-09-01", [outcome()]),
            ("2026-09-02", [outcome(False) for _ in range(3)]),
            ("2026-09-03", []),
        ]:
            days.append({"plan_date": stamp, "outcomes": rows, "summary": summarize(rows)})
        summary = aggregate_days(days)
        self.assertEqual(summary["accuracy_pct"], 25)
        self.assertEqual(summary["mean_daily_accuracy_pct"], 50)
        self.assertEqual(summary["empty_days"], 1)
        self.assertEqual(group_periods(days, "week")[0]["accuracy_pct"], 25)
        month = group_periods(days, "month")[0]
        self.assertEqual(month["period"], "2026-09")
        self.assertEqual(month["observed_count"], 4)

    def test_replay_blocks_future_and_does_not_mutate_frozen_inputs(self):
        snapshot = {
            "pools": {"2026-09-23": [{"code": "600001", "industry": "旧分类"}],
                      "2026-09-24": [{"code": "600002"}]},
            "broken": {}, "concepts": {}, "concept_sizes": {}, "industry_codes": {},
            "calendar": ["2026-09-23", "2026-09-24"],
        }
        original = copy.deepcopy(snapshot)
        def assign(provider, reference):
            self.assertTrue(all(day <= reference for day in provider._limit_pools))
            provider._limit_pools[reference][0]["industry"] = "当日计算"
        with patch("banxia_strategy.research_data.MootdxProvider._assign_themes", assign):
            provider = DatedResearchProvider(snapshot, date(2026, 9, 23))
        self.assertEqual(snapshot, original)
        with self.assertRaises(ValueError):
            provider.limit_up_pool(date(2026, 9, 24))
        with self.assertRaises(ValueError):
            provider.broken_board_pool(date(2026, 9, 24))
        first = provider.limit_up_pool(date(2026, 9, 23))
        first[0]["industry"] = "外部修改"
        self.assertEqual(provider.limit_up_pool(date(2026, 9, 23))[0]["industry"], "当日计算")


class ResearchSelectionTest(unittest.TestCase):
    def test_small_sample_and_missing_labels_cannot_win(self):
        baseline = {"observed_count": 20, "active_days": 10, "missing_count": 0}
        self.assertFalse(enough_samples({**baseline, "observed_count": 11}, baseline))
        self.assertFalse(enough_samples({**baseline, "active_days": 5}, baseline))
        self.assertFalse(enough_samples({**baseline, "missing_count": 1}, baseline))
        self.assertTrue(enough_samples(baseline, baseline))

    def test_grid_preserves_timing_and_risk_at_score_upper_boundary(self):
        base = StrategyConfig.from_mapping({"minimum_score": 999})
        for spec in experiment_specs(base):
            config = StrategyConfig.from_mapping(spec["config"])
            self.assertLessEqual(config.minimum_score, 1000)
            for key in ("entry_cutoff_time", "entry_open_min_pct", "entry_open_max_pct",
                        "position_limit_pct", "portfolio_risk_limit_pct"):
                self.assertEqual(getattr(config, key), getattr(base, key))
            self.assertGreaterEqual(config.minimum_amount_cny, base.minimum_amount_cny)

    def test_selection_is_fixed_before_holdout_and_failure_is_saved_separately(self):
        calendar = [str(date(2026, 9, 1) + timedelta(days=index)) for index in range(17)]
        snapshot = {
            "calendar": calendar, "requested_start": calendar[0], "requested_end": calendar[-2],
            "collected_at": "2026-09-25T12:00:00+08:00", "quality": {"limitations": []},
        }
        base = StrategyConfig()
        specs = []
        for index, score in enumerate((58, 62, 66)):
            config = asdict(StrategyConfig.from_mapping({"minimum_score": score}))
            specs.append({"trial_id": f"trial-{index}", "config": config,
                          "changes": {} if index == 0 else {"minimum_score": {"before": 58, "after": score}}})
        captured = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def evaluate(_snapshot, references, config, **_kwargs):
                phase = "training" if len(references) == 6 else "validation" if len(references) == 4 else "holdout"
                if phase == "holdout":
                    selection = next(root.glob("*/selection-before-holdout.json"))
                    captured.append(json.loads(selection.read_text())["trial_id"])
                # 62 wins validation; 66 wins holdout. The latter must NOT affect selection.
                hits = {58: 1, 62: 2, 66: 1} if phase != "holdout" else {58: 2, 62: 0, 66: 3}
                days = []
                for ref in references:
                    rows = [outcome(index < hits[config.minimum_score]) for index in range(3)]
                    days.append({
                        "reference_date": ref, "plan_date": calendar[calendar.index(ref) + 1],
                        "report": {"strategy_config": asdict(config)},
                        "outcomes": rows, "summary": summarize(rows),
                    })
                summary = aggregate_days(days)
                summary["mean_weekly_accuracy_pct"] = summary["accuracy_pct"]
                return {"days": days, "summary": summary,
                        "weeks": group_periods(days, "week"), "months": group_periods(days, "month")}
            with patch("banxia_strategy.research.experiment_specs", return_value=specs), patch(
                "banxia_strategy.research.evaluate_config", side_effect=evaluate,
            ), redirect_stdout(io.StringIO()):
                result, output = optimize(snapshot, base, root)
            self.assertTrue(captured)
            self.assertEqual(set(captured), {"trial-1"})
            self.assertEqual(result["strategy"]["selected_trial"], "trial-1")
            self.assertEqual(result["strategy"]["status"], "research_only")
            self.assertFalse(result["strategy"]["active"])
            self.assertLess(result["optimized"]["holdout"]["accuracy_pct"], result["baseline"]["holdout"]["accuracy_pct"])
            self.assertTrue((output / "optimized-strategy.json").is_file())
            self.assertEqual(base.minimum_score, 58)


class ResearchPersistenceTest(unittest.TestCase):
    def test_sql_transaction_targets_only_research_tables_and_retains_null_accuracy(self):
        from test_storage_adapters import FakeConnection, FakeCursor
        from banxia_strategy.adapters.postgres import PostgresStorage
        cursor = FakeCursor([])
        repository = PostgresStorage(connection_factory=lambda: FakeConnection(cursor))
        day = {"reference_date": "2026-09-24", "plan_date": "2026-09-25", "summary": summarize([])}
        payload = {
            "run_id": "00000000-0000-0000-0000-000000000001",
            "start_date": "2026-08-25", "end_date": "2026-09-24",
            "created_at": "2026-09-25T12:00:00+08:00", "input_sha256": "a"*64,
            "baseline": {"days": [day]}, "optimized": {"days": [day]},
            "strategy": {"strategy_id": "research-one", "revision": "b"*64, "config": {}},
        }
        save_research(repository, payload, [])
        self.assertEqual(len(cursor.calls), 4)
        for sql, _ in cursor.calls:
            self.assertTrue(sql.startswith("INSERT INTO banxia.research_"))
        self.assertIsNone(cursor.calls[1][1][4])

    def test_local_download_blocks_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalResearchStore(directory)
            with self.assertRaises(ValueError):
                store.get_research_run("../config")
            self.assertIsNone(store.asset_path("00000000-0000-0000-0000-000000000001", "../../strategy.json"))


if __name__ == "__main__":
    unittest.main()
