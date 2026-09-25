"""Transactional PostgreSQL checks; all fixtures roll back, including API writes."""
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from banxia_strategy.adapters.postgres import PostgresStorage
from banxia_strategy.adapters.strategy_catalog import CatalogConfigStore
from banxia_strategy.api import ApiServices, create_api_app
from banxia_strategy.application.strategy_engine import StrategyEventProcessor
from banxia_strategy.application.report_worker import ReportWorker
from banxia_strategy.application.persistence import PersistenceResult
from banxia_strategy.storage_config import StorageSettings
from banxia_strategy.contracts.topics import MARKET_QUOTE_SNAPSHOT
from banxia_strategy.domain.events import EventEnvelope
from banxia_strategy.ports.messaging import ConsumedEvent
from banxia_strategy.strategy import StrategyEngine
from banxia_strategy.strategy_config import ConfigConflict, ConfigError, StrategyConfig, StrategyConfigStore
from banxia_strategy.web_server import ReportStore
from test_api_v1 import FakeCache
from test_strategy import FakeProvider


@unittest.skipUnless(os.environ.get("BANXIA_TEST_POSTGRES_DSN"), "set BANXIA_TEST_POSTGRES_DSN for database isolation tests")
class MultiStrategyTest(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.connection = psycopg.connect(os.environ["BANXIA_TEST_POSTGRES_DSN"])
        self.addCleanup(self.connection.close)
        self.addCleanup(self.connection.rollback)
        @contextmanager
        def factory():
            yield self.connection
        self.repository = PostgresStorage(connection_factory=factory)
        self.a = self.repository.create_strategy("隔离测试 A", {})
        self.b = self.repository.create_strategy("隔离测试 B", {})
        self.report = StrategyEngine(FakeProvider(), StrategyConfig()).run(date(2026, 9, 23)).to_dict()
        self.report["next_session"] = "2026-09-24"
        self.assertTrue(self.report["candidates"])

    def persist(self, strategy, version="test-v1"):
        report = {**self.report, "strategy_id": strategy["strategy_id"], "strategy_code": strategy["code"],
                  "strategy_name": strategy["name"], "strategy_version": version}
        return self.repository.persist_report(report, strategy_version=version, strategy_config={},
                                              code_commit="test", enqueue_events=True)

    def test_two_strategies_daily_unique_and_refresh_is_scoped(self):
        first = self.persist(self.a)
        other = self.persist(self.b)
        updated = self.persist(self.a, "test-v2")
        self.persist(self.a, "test-v2")
        self.assertNotEqual(first.plan_id, updated.plan_id)
        self.assertEqual(self.repository.get_active_plan(self.report["next_session"], self.a["strategy_id"])["plan_id"], updated.plan_id)
        self.assertEqual(self.repository.get_active_plan(self.report["next_session"], self.b["strategy_id"])["plan_id"], other.plan_id)
        for strategy in (self.a, self.b):
            rows = self.repository.list_strategy_days(strategy["strategy_id"])
            self.assertEqual(len([r for r in rows if r["trade_date"] == self.report["as_of"]]), 1)
            day = self.repository.get_strategy_day(strategy["strategy_id"], self.report["next_session"])
            self.assertIsNotNone(day["execution_plan"])
            self.assertEqual(day["actual_status"], "pending")

    def test_gap_fill_does_not_replace_existing_plan_or_execution(self):
        report = {
            **self.report,
            "strategy_id": self.a["strategy_id"],
            "strategy_code": self.a["code"],
            "strategy_name": self.a["name"],
            "strategy_version": "historical-v1",
        }
        self.repository.fill_daily_report_gaps(self.a["strategy_id"], report)
        changed = {**report, "strategy_version": "must-not-replace"}
        self.repository.fill_daily_report_gaps(self.a["strategy_id"], changed)
        reference = self.repository.get_strategy_day(self.a["strategy_id"], report["as_of"])
        execution = self.repository.get_strategy_day(self.a["strategy_id"], report["next_session"])
        self.assertEqual(reference["next_plan"]["strategy_version"], "historical-v1")
        self.assertEqual(execution["execution_plan"]["strategy_version"], "historical-v1")

    def test_shared_quote_produces_two_independent_idempotent_decisions(self):
        ids = [self.persist(s) for s in (self.a, self.b)]
        candidate = {**self.report["candidates"][0], "plan_date": self.report["next_session"]}
        when = datetime.fromisoformat(candidate["plan_date"] + "T09:40:00+08:00")
        ref = candidate["latest_price"]
        event = EventEnvelope.create(
            event_type=MARKET_QUOTE_SNAPSHOT, producer="test", occurred_at=when, identity={"test": self.a["strategy_id"]},
            payload={"symbol": candidate["code"], "trade_date": candidate["plan_date"], "source_time": when,
                     "collected_at": when, "price": ref * 1.03, "open": ref * 1.02, "low": ref,
                     "high": ref * 1.03, "previous_close": ref},
        )
        consumed = ConsumedEvent(MARKET_QUOTE_SNAPSHOT, 1, 999, candidate["code"], event)
        for strategy, identity in zip((self.a, self.b), ids):
            processor = StrategyEventProcessor(repository=self.repository, plan_id=identity.plan_id,
                strategy_version_id=identity.strategy_version_id, candidates=[candidate])
            self.assertIsNotNone(processor.process(consumed))
            self.assertIsNone(processor.process(consumed))
            state = self.repository.get(identity.plan_id, candidate["code"])
            self.assertEqual(state.version, 1)
            day = self.repository.get_strategy_day(strategy["strategy_id"], candidate["plan_date"])
            self.assertEqual(day["actual_status"], "live")
            self.assertIn(candidate["code"], day["actuals"]["stocks"])
            processor.candidates[candidate["code"]]["plan_date"] = "2099-01-01"
            self.assertIsNone(processor.process(consumed))

    def test_config_is_immutable_and_new_strategy_records_lineage(self):
        a = CatalogConfigStore(self.repository, self.a["strategy_id"])
        b = CatalogConfigStore(self.repository, self.b["strategy_id"])
        old = a.payload()
        with self.assertRaises(ConfigConflict):
            a.save({**old["config"], "minimum_score": 70}, old["revision"])
        self.assertEqual(b.read().minimum_score, 58)
        child = self.repository.create_strategy(
            "隔离测试 A 新版本", {**old["config"], "minimum_score": 70},
            parent_strategy_id=self.a["strategy_id"],
            config_changes={"minimum_score": {"from": 58, "to": 70}},
        )
        self.assertEqual(child["parent_strategy_id"], self.a["strategy_id"])
        self.assertEqual(child["config_changes"]["minimum_score"]["to"], 70)
        self.assertFalse(child["enabled"])

    def test_api_copy_update_refresh_and_empty_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_api_app(ApiServices(
                ReportStore([Path(tmp)]), self.repository, FakeCache(),
                config_store=StrategyConfigStore(Path(tmp) / "strategy.json"))))
            source = client.get(f"/api/v1/strategy-config?strategy_id={self.a['strategy_id']}").json()
            response = client.post("/api/v1/strategies", json={
                "name": "API 副本", "parent_strategy_id": self.a["strategy_id"],
                "config": source["config"], "revision": source["revision"],
            })
            self.assertEqual(response.status_code, 201)
            sid = response.json()["strategy_id"]
            self.assertEqual(client.patch(f"/api/v1/strategies/{sid}", json={"enabled": True}).status_code, 200)
            self.assertFalse(self.repository.get_strategy(self.a["strategy_id"])["enabled"])
            self.assertTrue(self.repository.get_strategy(sid)["enabled"])
            self.assertEqual(client.patch(f"/api/v1/strategies/{sid}", json={"enabled": False}).status_code, 200)
            self.assertFalse(self.repository.get_strategy(sid)["enabled"])
            job = client.post(f"/api/v1/reports/2026-09-23/refresh?strategy_id={sid}")
            self.assertEqual(job.status_code, 409)
            self.report["candidates"] = []
            self.persist(self.b)
            rows = client.get(f"/api/v1/strategies/{self.b['strategy_id']}/days").json()["items"]
            self.assertEqual(next(r for r in rows if r["trade_date"] == self.report["next_session"])["actual_status"], "no_candidates")
            self.assertEqual(client.get("/api/v1/strategies/bad/days").status_code, 400)

    def test_corrected_calendar_removes_empty_nontrading_record(self):
        self.repository.save_trading_sessions(["2026-09-24", "2026-09-25", "2026-09-28"])
        self.repository.ensure_strategy_days("2026-09-25")
        self.assertIsNotNone(self.repository.get_strategy_day(self.a["strategy_id"], "2026-09-25"))
        self.repository.save_trading_sessions(["2026-09-24", "2026-09-28"])
        self.assertIsNone(self.repository.get_strategy_day(self.a["strategy_id"], "2026-09-25"))

    def test_custom_report_worker_uses_each_immutable_strategy_config(self):
        store = CatalogConfigStore(self.repository, self.a["strategy_id"])
        with tempfile.TemporaryDirectory() as tmp, patch(
            "banxia_strategy.application.report_worker.PostgresStorage", return_value=self.repository
        ), patch("banxia_strategy.application.report_worker.write_completion_marker"), patch(
            "banxia_strategy.application.report_worker.persist_report_copy", return_value=PersistenceResult(False)
        ):
            worker = ReportWorker(strategy_config_path=Path(tmp) / "unused.json",
                output_dir=Path(tmp), storage_settings=StorageSettings(), provider=FakeProvider(),
                strategy_id=self.a["strategy_id"])
            original, paths, _ = worker.run(date(2026, 9, 23))
            self.assertIn(self.a["strategy_id"], str(paths["json"]))
            payload = store.payload()
            child = self.repository.create_strategy(
                "严格题材策略", {**payload["config"], "minimum_industry_limit_up_count": 20},
                parent_strategy_id=self.a["strategy_id"],
            )
            worker.strategy_id = child["strategy_id"]
            updated, _, _ = worker.run(date(2026, 9, 23))
            self.assertTrue(original.candidates)
            self.assertFalse(updated.candidates)
            self.assertNotEqual(original.strategy_version, updated.strategy_version)
