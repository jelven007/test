from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from banxia_strategy.api import ApiServices, create_api_app
from banxia_strategy.application.collector import MarketCollector
from banxia_strategy.application.plans import ActivePlan
from banxia_strategy.application.report_worker import ReportWorker
from banxia_strategy.application.persistence import PersistenceResult
from banxia_strategy.application.strategy_engine import StrategyEventProcessor
from banxia_strategy.config import RuntimeSettings
from banxia_strategy.domain.intraday import evaluate
from banxia_strategy.intraday import plan_for
from banxia_strategy.ports.messaging import ConsumedEvent
from banxia_strategy.ports.storage import ReportIdentity
from banxia_strategy.service import _config_store, _consume_report_refresh, _generate_scheduled_report, _prepare_plan
from banxia_strategy.storage_config import StorageSettings
from banxia_strategy.strategy import StrategyEngine
from banxia_strategy.strategy_config import (
    ConfigConflict, ConfigError, StrategyConfig, StrategyConfigStore, version_for,
)
from banxia_strategy.web_server import ReportStore
from test_api_v1 import FakeCache, FakeRepository
from test_strategy import FakeProvider, row
from test_pipeline import FakeDecisionRepository, STAMP, event


class StrategyConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "strategy.json"
        self.path.write_text("{}", encoding="utf-8")
        self.store = StrategyConfigStore(self.path)

    def test_invalid_values_cannot_change_file(self):
        invalid_values = [
            {"minimum_score": float("nan")}, {"hard_stop_pct": float("inf")},
            {"lookback_sessions": True}, {"lookback_sessions": 2.5},
            {"maximum_break_count": -1}, {"position_limit_pct": 70},
            {"entry_open_min_pct": 8}, {"entry_cutoff_time": "12:00"},
            {"entry_cutoff_time": "25:00"}, {"report_schedule": "16:20,16:20"},
            {"report_schedule": ""}, {"quote_interval_seconds": 0},
            {"minimum_amount_cny": 2e9}, {"main_board_only": False},
            {"minimum_sector_sample_size": 1}, {"unknown": 1},
            {"market_breadth_weight": 0, "market_break_weight": 0, "market_height_weight": 0},
        ]
        original = self.path.read_bytes()
        payload = self.store.payload()
        for changes in invalid_values:
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                self.store.save({**payload["config"], **changes}, payload["revision"])
            self.assertEqual(self.path.read_bytes(), original)

    def test_save_survives_restart_and_revision_conflict(self):
        payload = self.store.payload()
        changed = {**payload["config"], "minimum_score": 63, "entry_cutoff_time": "09:50"}
        saved = self.store.save(changed, payload["revision"])
        restarted = StrategyConfigStore(self.path)
        self.assertEqual(restarted.read().minimum_score, 63)
        self.assertEqual(restarted.read().entry_cutoff_time, "09:50")
        self.assertEqual(restarted.payload()["revision"], saved["revision"])
        with self.assertRaises(ConfigConflict):
            restarted.save(payload["config"], payload["revision"])
        self.assertEqual(restarted.read().minimum_score, 63)

    def test_concurrent_writers_cannot_overwrite_each_other(self):
        payload = self.store.payload()
        barrier = threading.Barrier(2)
        def save_score(score):
            barrier.wait()
            try:
                StrategyConfigStore(self.path).save({**payload["config"], "minimum_score": score}, payload["revision"])
                return True
            except ConfigConflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(save_score, [60, 61]))
        self.assertEqual(sorted(result), [False, True])
        self.assertIn(self.store.read().minimum_score, [60, 61])

    def test_api_save_errors_restart_and_authentication(self):
        services = ApiServices(ReportStore([]), FakeRepository(), FakeCache(), config_store=self.store)
        client = TestClient(create_api_app(services))
        response = client.get("/strategy")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        payload = client.get("/api/v1/strategy-config").json()
        changed = {**payload["config"], "minimum_score": 66}
        saved = client.put("/api/v1/strategy-config", json={"config": changed, "revision": payload["revision"]})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(client.put("/api/v1/strategy-config", json={"config": changed, "revision": payload["revision"]}).status_code, 409)
        invalid = client.put("/api/v1/strategy-config", json={
            "config": {**changed, "entry_cutoff_time": "09:00"}, "revision": saved.json()["revision"],
        })
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("entry_cutoff_time", invalid.json()["error"]["details"]["fields"])
        restarted = TestClient(create_api_app(services))
        self.assertEqual(restarted.get("/api/v1/strategy-config").json()["config"]["minimum_score"], 66)
        services.api_token = "test-token"
        protected = TestClient(create_api_app(services))
        self.assertEqual(protected.put("/api/v1/strategy-config", json={}).status_code, 401)

    def test_runtime_settings_reload_and_environment_fallback(self):
        settings = RuntimeSettings(strategy_config_path=self.path, report_schedule=("16:20",))
        store = _config_store(settings)
        self.assertEqual(store.read().report_schedule, "16:20")
        collector = MarketCollector(candidates=[{"code": "600001"}], publisher=Mock(), source=Mock(), config_store=store)
        payload = store.payload()
        store.save({**payload["config"], "quote_interval_seconds": 2, "bar_interval_seconds": 30, "report_schedule": "16:40"}, payload["revision"])
        collector.reload_intervals()
        self.assertEqual(collector.quote_interval_seconds, 2)
        self.assertEqual(collector.source.bar_interval, 30)
        self.assertEqual(_config_store(settings).read().report_schedule, "16:40")
        self.assertFalse(collector._active(datetime(2026, 9, 25, 12)))

    def test_new_filter_changes_candidates_without_mutating_old_report(self):
        provider = FakeProvider()
        config = StrategyConfig(minimum_score=0)
        original = StrategyEngine(provider, config).run(date(2026, 9, 23))
        original_payload = original.to_dict()
        self.assertTrue(original.candidates)
        changed = replace(config, minimum_industry_limit_up_count=10)
        self.assertEqual(StrategyEngine(provider, changed).run(date(2026, 9, 23)).candidates, [])
        self.assertEqual(original.to_dict(), original_payload)
        self.assertEqual(original.strategy_config["minimum_industry_limit_up_count"], 2)

    def test_score_weights_change_ranking(self):
        provider = FakeProvider()
        provider.pools[date(2026, 9, 23)] = [
            row("600001", "早封", "主题", first="09:31", seal=0),
            row("600002", "强封单", "主题", first="10:30", seal=108_000_000),
        ]
        config = StrategyConfig(minimum_score=0, max_per_industry=2, early_weight=100, seal_weight=0)
        early = StrategyEngine(provider, config).run(date(2026, 9, 23))
        sealed = StrategyEngine(provider, replace(config, early_weight=0, seal_weight=100)).run(date(2026, 9, 23))
        self.assertEqual(early.candidates[0].code, "600001")
        self.assertEqual(sealed.candidates[0].code, "600002")

    def test_st_support_does_not_satisfy_theme_threshold(self):
        provider = FakeProvider()
        provider.pools[date(2026, 9, 23)] = [row("600001", "普通股", "孤立"), row("600002", "ST样本", "孤立")]
        self.assertEqual(StrategyEngine(provider, StrategyConfig(minimum_score=0)).run(date(2026, 9, 23)).candidates, [])

    def test_snapshot_cutoff_and_low_rule_override_old_prose(self):
        candidate = {
            "latest_price": 10, "entry_trigger": "位于0.5%～5%", "invalidation": "低于-2%或高于7%",
            "plan": StrategyConfig(entry_cutoff_time="09:45", reject_below_previous_close=False).entry_rules(),
        }
        plan = plan_for(candidate)
        quote = {
            "price": 10.5, "open_change_pct": 2, "low": 9.9, "previous_close": 10,
            "fresh": True, "bid": 10.5, "ask": 10.51, "bid_volume": 100,
        }
        now = datetime(2026, 9, 25, 9, 44, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(evaluate(quote, plan, now, "2026-09-25")["state"], "watch")
        self.assertEqual(evaluate(quote, plan, now.replace(minute=45), "2026-09-25")["state"], "window_closed")
        self.assertEqual(evaluate(quote, {**plan, "reject_below_previous_close": True}, now, "2026-09-25")["state"], "reject_low")

    def test_version_changes_with_parameters_or_code(self):
        config = asdict(StrategyConfig())
        version = version_for(config, "v2", "abc")
        self.assertEqual(version_for(dict(reversed(list(config.items()))), "v2", "abc"), version)
        self.assertNotEqual(version_for({**config, "minimum_score": 80}, "v2", "abc"), version)
        self.assertNotEqual(version_for(config, "v2", "def"), version)

    def test_sector_confirmation_uses_plan_snapshot(self):
        feature = event("market.feature.realtime.v1", {
            "symbol": "600001", "source_time": STAMP, "sector_rise_ratio": 0.6,
            "attributes": {"sector_sample_size": 4},
        })
        quote = event("market.quote.snapshot.v1", {
            "symbol": "600001", "source_time": STAMP, "collected_at": STAMP,
            "price": 10.99, "open": 10.2, "high": 10.99, "low": 10.1, "previous_close": 10,
        })
        states = []
        for ratio, sample_size in [(0.5, 2), (0.8, 2), (0.5, 5)]:
            candidate = {
                "code": "600001", "latest_price": 10, "plan_date": "2026-09-24",
                "plan": StrategyConfig(minimum_sector_rise_ratio=ratio, minimum_sector_sample_size=sample_size).entry_rules(),
            }
            processor = StrategyEventProcessor(repository=FakeDecisionRepository(), plan_id="plan",
                                               strategy_version_id="version", candidates=[candidate])
            processor.process(ConsumedEvent(feature.event_type, 0, 1, "600001", feature))
            result = processor.process(ConsumedEvent(quote.event_type, 0, 2, "600001", quote))
            states.append(result.payload["state"])
        self.assertEqual(states, ["near_limit", "watch", "watch"])

    def test_startup_preserves_historical_active_plan(self):
        repository = Mock()
        repository.get_active_plan.return_value = {
            "reference_date": "2026-09-23", "run_id": "run", "plan_id": "plan",
            "strategy_version": "original", "strategy_version_id": "original-id",
        }
        with patch("banxia_strategy.service._load_plan", return_value=ActivePlan(
            report={"as_of": "2026-09-23", "next_session": "2026-09-24"}, candidates=(),
        )):
            plan, identity = _prepare_plan(repository, RuntimeSettings(strategy_config_path=self.path))
        repository.persist_report.assert_not_called()
        self.assertEqual(identity.strategy_version_id, "original-id")
        self.assertEqual(plan.report["strategy_version"], "original")

    def test_report_worker_reads_saved_parameters_for_each_run(self):
        worker = ReportWorker(strategy_config_path=self.path, output_dir=Path(self.temp.name) / "reports",
                              storage_settings=StorageSettings(), provider=FakeProvider())
        with patch("banxia_strategy.application.report_worker.persist_report_copy") as persist, patch(
            "banxia_strategy.application.report_worker.write_completion_marker"
        ):
            original, _, _ = worker.run(date(2026, 9, 23))
            payload = self.store.payload()
            self.store.save({**payload["config"], "minimum_industry_limit_up_count": 20}, payload["revision"])
            changed, _, _ = worker.run(date(2026, 9, 23))
        self.assertTrue(original.candidates)
        self.assertFalse(changed.candidates)
        self.assertNotEqual(original.strategy_version, changed.strategy_version)
        self.assertEqual(persist.call_args.args[0]["strategy_config"]["minimum_industry_limit_up_count"], 20)

    def test_schedule_and_queued_page_refresh_use_latest_saved_strategy(self):
        output = Path(self.temp.name) / "reports"
        settings = RuntimeSettings(strategy_config_path=self.path, report_output_dir=output)
        repository = FakeRepository()
        services = ApiServices(ReportStore([output]), repository, FakeCache(), config_store=self.store)
        client = TestClient(create_api_app(services))
        stop = Mock()
        stop.wait.return_value = False
        scheduled_at = datetime(2026, 9, 23, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        identity = ReportIdentity("run-id", "version-id", "plan-id")
        with patch("banxia_strategy.application.report_worker.MootdxProvider", return_value=FakeProvider()), patch(
            "banxia_strategy.application.report_worker.persist_report_copy",
            return_value=PersistenceResult(enabled=True, identity=identity),
        ):
            original_result = _generate_scheduled_report(settings, Mock(), scheduled_at.date(), scheduled_at, stop)
            original = client.get("/api/v1/reports/2026-09-23").json()
            self.assertTrue(original["candidates"])
            self.assertEqual(original["strategy_revision"], original_result["strategy_revision"])

            # Queue a refresh of the historical date, then change configuration
            # before the scheduler claims it. The queue must not freeze old rules.
            job = client.post("/api/v1/reports/2026-09-23/refresh").json()
            payload = client.get("/api/v1/strategy-config").json()
            saved = client.put("/api/v1/strategy-config", json={
                "config": {**payload["config"], "minimum_industry_limit_up_count": 20},
                "revision": payload["revision"],
            })
            self.assertEqual(saved.status_code, 200)
            repository.claim_report_refresh = Mock(return_value=job)
            repository.finish_report_refresh = Mock()
            self.assertTrue(_consume_report_refresh(repository, settings, Mock(), stop))
            refreshed = client.get("/api/v1/reports/2026-09-23").json()
            self.assertFalse(refreshed["candidates"])
            self.assertEqual(refreshed["strategy_revision"], saved.json()["revision"])
            self.assertNotEqual(refreshed["strategy_version"], original["strategy_version"])
            completed = repository.finish_report_refresh.call_args.kwargs["result"]
            self.assertEqual(completed["strategy_revision"], saved.json()["revision"])
            self.assertEqual(completed["generated_at"], refreshed["generated_at"])
            self.assertEqual(completed["strategy_version"], refreshed["strategy_version"])

            # The next timed run also reloads the changed file, using the same worker.
            next_result = _generate_scheduled_report(settings, Mock(), scheduled_at.date(), scheduled_at, stop)
            self.assertEqual(next_result["strategy_revision"], saved.json()["revision"])
            self.assertEqual(original["strategy_config"]["minimum_industry_limit_up_count"], 2)

    def test_refresh_never_falls_back_to_old_report_when_latest_config_is_invalid(self):
        output = Path(self.temp.name) / "reports"
        settings = RuntimeSettings(strategy_config_path=self.path, report_output_dir=output)
        self.path.write_text('{"minimum_score": -1}', encoding="utf-8")
        repository = Mock()
        repository.claim_report_refresh.return_value = {"job_id": "invalid-config", "payload": {"trade_date": "2026-09-23"}}
        stop = Mock()
        stop.wait.return_value = False
        with patch("banxia_strategy.application.report_worker.MootdxProvider", return_value=FakeProvider()), patch(
            "banxia_strategy.application.report_worker.persist_report_copy"
        ) as persist:
            _consume_report_refresh(repository, settings, Mock(), stop)
        self.assertFalse(repository.finish_report_refresh.call_args.kwargs["succeeded"])
        persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
