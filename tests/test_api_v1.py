from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from banxia_strategy.adapters.postgres import STRATEGY_CODE
from banxia_strategy.adapters.strategy_catalog import CatalogConfigStore
from banxia_strategy.api import ApiServices, create_api_app
from banxia_strategy.strategy_config import StrategyConfig
from banxia_strategy.web_server import ReportStore


class FakeRepository:
    def __init__(self):
        self.jobs = {}

    def ready(self):
        return True

    def get_active_plan(self, trade_date=None):
        if trade_date not in (None, "2026-09-24"):
            return None
        return {
            "plan_id": "plan",
            "reference_date": "2026-09-23",
            "trade_date": "2026-09-24",
            "strategy_version": "v1",
            "candidates": [
                {
                    "symbol": "002635",
                    "name": "安洁科技",
                    "industry": "电子",
                }
            ],
        }

    def list_decision_events(self, **_kwargs):
        return ({"event_id": "decision-1", "state": "watch"},)

    def get_strategy_run(self, run_id):
        return {"run_id": run_id, "status": "succeeded"} if run_id == "run" else None

    def enqueue_report_refresh(self, trade_date, *, requested_by="web"):
        job_id = f"00000000-0000-0000-0000-{len(self.jobs) + 1:012d}"
        job = {
            "job_id": job_id,
            "job_type": "report_refresh",
            "status": "queued",
            "attempt": 0,
            "payload": {
                "trade_date": trade_date,
                "requested_by": requested_by,
            },
            "result": None,
            "error": None,
            "created_at": "2026-09-25T09:00:00+08:00",
            "started_at": None,
            "finished_at": None,
        }
        self.jobs[job_id] = job
        return job

    def get_job_execution(self, job_id):
        return self.jobs.get(job_id)


class FakeCache:
    def ready(self):
        return True

    def get_latest_quote(self, symbol):
        return {
            "payload": {
                "symbol": symbol,
                "source_time": "2026-09-24T09:45:00+08:00",
                "collected_at": "2026-09-24T09:45:00+08:00",
                "price": 10.5,
                "open": 10.2,
                "previous_close": 10.0,
                "cumulative_amount_cny": 300000000,
            }
        }

    def get_latest_decision(self, plan_id, symbol):
        return {
            "payload": {
                "plan_id": plan_id,
                "symbol": symbol,
                "state": "watch",
                "label": "观察",
                "reason_code": "watch",
                "reason": "等待确认",
                "irreversible": False,
                "updated_at": "2026-09-24T09:45:00+08:00",
            }
        }

    def get_latest_feature(self, symbol):
        return {
            "payload": {
                "symbol": symbol,
                "minute_volume_ratio": 2.5,
                "sector_rise_ratio": 0.75,
                "attributes": {"sector_sample_size": 4},
                "data_state": "fresh",
            }
        }

    def get_minute_bars(self, symbol, limit=240):
        return (
            {
                "payload": {
                    "trade_date": "2026-09-24",
                    "symbol": symbol,
                    "bar_time": "2026-09-24T09:45:00+08:00",
                    "close": 10.5,
                    "volume": 100,
                }
            },
        )


def write_report(root: Path):
    directory = root / "2026-09-23"
    directory.mkdir()
    payload = {
        "as_of": "2026-09-23",
        "next_session": "2026-09-24",
        "generated_at": "2026-09-23T16:20:00+08:00",
        "market": {
            "regime": "中性",
            "score": 60,
            "limit_up_count": 20,
            "max_board": 3,
        },
        "candidates": [
            {
                "code": "002635",
                "name": "安洁科技",
                "rank": 1,
                "score": 80,
                "entry_trigger": "test",
                "invalidation": "test",
            }
        ],
        "disclaimer": "research only",
    }
    (directory / "candidates.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    (directory / "report.md").write_text("# report", encoding="utf-8")


class FakeStrategyRepository:
    def __init__(self, *, code=STRATEGY_CODE):
        self.config = asdict(StrategyConfig())
        self.deleted = None
        self.created = None
        self.history_job = None
        self.item = {
            "strategy_id": "00000000-0000-0000-0000-000000000010",
            "code": code,
            "name": "首板晋级二板策略",
            "description": "",
            "enabled": True,
            "archived": False,
            "config": self.config.copy(),
            "parent_strategy_id": None,
            "config_changes": {},
        }

    def list_strategies(self):
        return [self.item.copy()] if self.item else []

    def get_strategy(self, strategy_id):
        return self.item.copy() if self.item and strategy_id == self.item["strategy_id"] else None

    def rename_strategy(self, strategy_id, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("策略名称需为 1 至 80 个字符")
        self.item["name"] = name.strip()
        return self.item.copy()

    def update_strategy(self, strategy_id, *, enabled, archived=False):
        self.item["enabled"] = enabled
        self.item["archived"] = archived
        return self.item.copy()

    def create_strategy(self, name, config, **kwargs):
        self.created = {"name": name, "config": config, **kwargs}
        self.item = {
            **self.item,
            "strategy_id": "00000000-0000-0000-0000-000000000011",
            "code": "custom-00000000-0000-0000-0000-000000000011",
            "name": name,
            "enabled": kwargs.get("enabled", False),
            "config": config,
            "parent_strategy_id": kwargs["parent_strategy_id"],
            "config_changes": kwargs["config_changes"],
        }
        materialization = kwargs["materialization"]
        self.history_job = {
            "job_id": "00000000-0000-0000-0000-000000000012",
            "job_type": "strategy_history",
            "status": "queued",
            "payload": {
                **materialization,
                "strategy_id": self.item["strategy_id"],
            },
            "result": None,
            "error": None,
        }
        return self.item.copy()

    def get_latest_strategy_history(self, strategy_id):
        return self.history_job if self.history_job and strategy_id == self.item["strategy_id"] else None

    def get_job_execution(self, job_id):
        return self.history_job if self.history_job and job_id == self.history_job["job_id"] else None

    def delete_strategy(self, strategy_id, *, cleanup=None):
        manifest = {
            "strategy_id": strategy_id,
            "plan_ids": ["00000000-0000-0000-0000-000000000020"],
            "trade_dates": ["2026-09-24"],
            "object_keys": ["reports/strategy/report.md"],
            "research_run_ids": [],
            "strategy_days": 1,
            "detached_children": 0,
        }
        if cleanup:
            cleanup(manifest)
        self.deleted = manifest
        self.item = None
        return manifest


class ApiV1Test(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        write_report(root)
        self.services = ApiServices(
            reports=ReportStore([root]),
            repository=FakeRepository(),
            cache=FakeCache(),
            kafka_ready=lambda: True,
            clickhouse_ready=lambda: True,
            strategy_version="v2",
        )
        self.client = TestClient(create_api_app(self.services))

    def tearDown(self):
        self.directory.cleanup()

    def test_health_monitor_reports_and_request_id(self):
        response = self.client.get(
            "/api/v1/health/ready",
            headers={"X-Request-ID": "request-1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Request-ID"], "request-1")
        self.assertEqual(response.json()["status"], "ready")

        monitor = self.client.get("/api/v1/monitor").json()
        self.assertEqual(monitor["plan_id"], "plan")
        self.assertEqual(monitor["stocks"][0]["symbol"], "002635")
        self.assertEqual(monitor["stocks"][0]["decision"]["state"], "watch")
        self.assertEqual(
            monitor["stocks"][0]["feature"]["minute_volume_ratio"],
            2.5,
        )
        self.assertEqual(len(monitor["stocks"][0]["candles"]), 1)

        reports = self.client.get("/api/v1/reports").json()
        self.assertEqual(reports["items"][0]["trade_date"], "2026-09-23")
        self.assertEqual(reports["items"][0]["strategy_version"], "v2")
        report = self.client.get("/api/v1/reports/2026-09-23").json()
        self.assertEqual(report["candidates"][0]["symbol"], "002635")
        self.assertEqual(report["strategy_version"], "v2")

    def test_monitor_does_not_mix_quotes_from_another_day(self):
        self.services.cache.get_latest_quote = Mock(return_value={
            "payload": {"source_time": "2026-09-25T09:45:00+08:00", "price": 99}
        })
        payload = self.client.get("/api/v1/monitor").json()
        self.assertIsNone(payload["stocks"][0]["price"])
        self.assertEqual(payload["data_status"]["state"], "unavailable")

    def test_report_refresh_creates_a_new_job_for_every_request(self):
        first = self.client.post(
            "/api/v1/reports/2026-09-23/refresh",
            headers={"X-Request-ID": "refresh-1"},
        )
        second = self.client.post("/api/v1/reports/2026-09-23/refresh")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertNotEqual(first.json()["job_id"], second.json()["job_id"])
        self.assertEqual(
            first.json()["payload"],
            {
                "trade_date": "2026-09-23",
                "requested_by": "refresh-1",
            },
        )
        status = self.client.get(
            f"/api/v1/report-jobs/{first.json()['job_id']}"
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "queued")

    def test_report_refresh_rejects_invalid_date_and_unknown_job(self):
        invalid = self.client.post("/api/v1/reports/not-a-date/refresh")
        malformed_job = self.client.get("/api/v1/report-jobs/missing")
        missing = self.client.get(
            "/api/v1/report-jobs/00000000-0000-0000-0000-999999999999"
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(malformed_job.status_code, 400)
        self.assertEqual(missing.status_code, 404)

    def test_web_assets_are_not_served_from_stale_browser_cache(self):
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.headers["Cache-Control"], "no-store")
        self.assertIn("/styles.css?v=20260925.1", dashboard.text)

        stylesheet = self.client.get("/styles.css?v=20260925.1")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertEqual(stylesheet.headers["Cache-Control"], "no-store")
        self.assertIn("@media (max-width: 480px)", stylesheet.text)

    def test_token_protects_non_health_api(self):
        self.services.api_token = "secret"
        client = TestClient(create_api_app(self.services))
        self.assertEqual(client.get("/api/v1/health/live").status_code, 200)
        denied = client.get("/api/v1/monitor")
        self.assertEqual(denied.status_code, 401)
        allowed = client.get(
            "/api/v1/monitor",
            headers={"Authorization": "Bearer secret"},
        )
        self.assertEqual(allowed.status_code, 200)

    def test_errors_and_metrics_use_public_contract(self):
        missing = self.client.get("/api/v1/reports/2026-01-01")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "NOT_FOUND")
        self.assertTrue(missing.json()["error"]["request_id"])

        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn(
            "banxia_http_requests_total",
            metrics.text,
        )

    def test_sse_request_is_injected_instead_of_exposed_as_query(self):
        operation = self.client.get("/api/openapi.json").json()["paths"][
            "/api/v1/monitor/stream"
        ]["get"]
        parameters = operation.get("parameters", [])
        self.assertNotIn("request", {item["name"] for item in parameters})

    def test_strategy_list_summary_and_name_only_patch(self):
        repository = FakeStrategyRepository()
        client = TestClient(create_api_app(ApiServices(
            reports=self.services.reports,
            repository=repository,
            cache=FakeCache(),
        )))
        listed = client.get("/api/v1/strategies")
        self.assertEqual(listed.status_code, 200)
        item = listed.json()["items"][0]
        self.assertEqual(item["key_parameters"]["minimum_score"], 58)
        self.assertEqual(item["key_parameters"]["entry_cutoff_time"], "10:00")
        self.assertNotIn("config", item)
        self.assertTrue(item["is_initial"])
        self.assertEqual(item["permissions"], {
            "edit_parameters": True,
            "save_as": True,
            "delete": False,
        })
        self.assertEqual(
            client.get("/api/v1/strategies?q=不存在").json(),
            {"items": [], "total": 0},
        )
        self.assertEqual(
            client.get("/api/v1/strategies?status=inactive").json()["total"],
            0,
        )
        self.assertEqual(
            client.get("/api/v1/strategies?status=invalid").status_code,
            400,
        )

        strategy_id = repository.item["strategy_id"]
        renamed = client.patch(
            f"/api/v1/strategies/{strategy_id}",
            json={"name": "首板晋级二板策略（严格版）"},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["name"], "首板晋级二板策略（严格版）")
        self.assertEqual(repository.item["config"], asdict(StrategyConfig()))

        for payload in (
            {"name": "组合修改", "enabled": False},
            {"config": {"minimum_score": 70}},
        ):
            response = client.patch(f"/api/v1/strategies/{strategy_id}", json=payload)
            self.assertEqual(response.status_code, 422)

    def test_strategy_save_as_requires_initial_parameter_change_and_queues_history(self):
        repository = FakeStrategyRepository()
        client = TestClient(create_api_app(ApiServices(
            reports=self.services.reports,
            repository=repository,
            cache=FakeCache(),
        )))
        source = client.get(
            f"/api/v1/strategy-config?strategy_id={repository.item['strategy_id']}"
        ).json()
        unchanged = client.post("/api/v1/strategies", json={
            "name": "无变化",
            "parent_strategy_id": repository.item["strategy_id"],
            "config": source["config"],
            "revision": source["revision"],
        })
        self.assertEqual(unchanged.status_code, 422)

        changed = {**source["config"], "minimum_score": 66}
        response = client.post("/api/v1/strategies", json={
            "name": "初始策略参数版本",
            "parent_strategy_id": repository.item["strategy_id"],
            "config": changed,
            "revision": source["revision"],
            "history_range": "1m",
        })
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["is_initial"])
        self.assertEqual(response.json()["history_generation"]["status"], "queued")
        self.assertEqual(repository.created["config"]["minimum_score"], 66)
        self.assertEqual(
            repository.created["materialization"]["datasets"],
            ["next_plan", "intraday_monitor"],
        )
        job_id = response.json()["history_generation"]["job_id"]
        self.assertEqual(
            client.get(f"/api/v1/strategy-jobs/{job_id}").status_code,
            200,
        )

    def test_initial_strategy_cannot_be_deleted_or_derived_from_custom_strategy(self):
        repository = FakeStrategyRepository()
        client = TestClient(create_api_app(ApiServices(
            reports=self.services.reports,
            repository=repository,
            cache=FakeCache(),
        )))
        self.assertEqual(
            client.delete(f"/api/v1/strategies/{repository.item['strategy_id']}").status_code,
            409,
        )

        repository.item["code"] = "custom-existing"
        payload = CatalogConfigStore(repository, repository.item["strategy_id"]).payload()
        changed = {**payload["config"], "minimum_score": 66}
        response = client.post("/api/v1/strategies", json={
            "name": "非法派生",
            "parent_strategy_id": repository.item["strategy_id"],
            "config": changed,
            "revision": payload["revision"],
        })
        self.assertEqual(response.status_code, 422)

    def test_strategy_delete_removes_external_data_and_local_reports(self):
        repository = FakeStrategyRepository(code="custom-delete")
        cache = FakeCache()
        cache.delete_strategy_data = Mock()
        object_store = Mock()
        root = Path(self.directory.name)
        strategy_id = repository.item["strategy_id"]
        local_report = root / "strategies" / strategy_id / "2026-09-23"
        local_report.mkdir(parents=True)
        (local_report / "report.md").write_text("# delete me", encoding="utf-8")
        client = TestClient(create_api_app(ApiServices(
            reports=ReportStore([root]),
            repository=repository,
            cache=cache,
            object_store=object_store,
        )))

        response = client.delete(f"/api/v1/strategies/{strategy_id}")

        self.assertEqual(response.status_code, 204)
        self.assertIsNone(repository.get_strategy(strategy_id))
        self.assertFalse(local_report.parent.exists())
        object_store.remove_objects.assert_called_once_with(
            ["reports/strategy/report.md"]
        )
        cache.delete_strategy_data.assert_called_once_with(
            strategy_id,
            ["00000000-0000-0000-0000-000000000020"],
            ["2026-09-24"],
        )

    def test_strategy_delete_reports_cleanup_failure(self):
        repository = FakeStrategyRepository(code="custom-delete")
        object_store = Mock()
        object_store.remove_objects.side_effect = RuntimeError("storage unavailable")
        client = TestClient(create_api_app(ApiServices(
            reports=self.services.reports,
            repository=repository,
            cache=FakeCache(),
            object_store=object_store,
        )))

        response = client.delete(
            f"/api/v1/strategies/{repository.item['strategy_id']}"
        )

        self.assertEqual(response.status_code, 503)
        self.assertIsNotNone(repository.item)

    def test_research_history_strategy_download_and_asset_stream(self):
        run_id = "00000000-0000-0000-0000-000000000001"
        payload = {
            "run_id": run_id, "strategy": {"config": {"minimum_score": 72}, "active": False},
            "assets": [{"filename": "report.md", "object_key": "research/hash/report.md",
                        "content_type": "text/markdown"}],
        }
        self.services.repository.list_research_runs = lambda: [{"run_id": run_id}]
        self.services.repository.get_research_run = lambda value: payload if value == run_id else None
        stream = Mock()
        stream.stream.return_value = iter([b"# saved ", b"experiment\n"])
        minio = Mock()
        minio.get_object.return_value = stream
        self.services.object_store = SimpleNamespace(client=minio, bucket="strategy-reports")
        root = f"/api/v1/research/{run_id}"
        self.assertEqual(self.client.get("/api/v1/research").json()["items"][0]["run_id"], run_id)
        self.assertFalse(self.client.get(root).json()["strategy"]["active"])
        download = self.client.get(root + "/strategy")
        self.assertEqual(download.json(), {"minimum_score": 72})
        self.assertIn("attachment;", download.headers["Content-Disposition"])
        self.assertEqual(download.headers["Cache-Control"], "no-store")
        asset = self.client.get(root + "/assets/report.md")
        self.assertEqual(asset.content, b"# saved experiment\n")
        minio.get_object.assert_called_once_with("strategy-reports", "research/hash/report.md")
        stream.close.assert_called_once()
        stream.release_conn.assert_called_once()
        self.assertEqual(self.client.get(root + "/assets/strategy.json").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/research/bad-id").status_code, 400)
        self.assertEqual(self.client.get(root[:-1] + "2").status_code, 404)
        page = self.client.get("/research")
        self.assertEqual(page.status_code, 200)
        self.assertIn("回测优化", page.text)
        self.services.api_token = "test-research"
        secured = TestClient(create_api_app(self.services))
        for suffix in ("", "/strategy", "/assets/report.md"):
            self.assertEqual(secured.get(root + suffix).status_code, 401)


if __name__ == "__main__":
    unittest.main()
