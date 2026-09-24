from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from banxia_strategy.api import ApiServices, create_api_app
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
                "source_time": "2099-01-02T09:45:00+08:00",
                "collected_at": "2099-01-02T09:45:00+08:00",
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


if __name__ == "__main__":
    unittest.main()
