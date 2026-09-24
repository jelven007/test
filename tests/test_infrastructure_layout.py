from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from banxia_strategy.domain import DecisionState


ROOT = Path(__file__).resolve().parents[1]


class StorageSchemaTest(unittest.TestCase):
    def test_postgres_schema_contains_transactional_control_tables(self):
        schema = (ROOT / "migrations/postgres/001_initial.sql").read_text(encoding="utf-8")
        for table in (
            "strategy_definition",
            "strategy_version",
            "strategy_run",
            "strategy_plan",
            "watchlist",
            "watchlist_item",
            "candidate",
            "decision_state",
            "decision_event",
            "inbox_event",
            "outbox_event",
            "report_asset",
            "job_execution",
            "audit_log",
        ):
            self.assertIn(f"banxia.{table}", schema)
        self.assertIn("UNIQUE (trade_date, strategy_version_id)", schema)
        for state in DecisionState:
            self.assertIn(f"'{state.value}'", schema)

    def test_decision_states_match_api_and_event_contracts(self):
        contracts = "\n".join(
            [
                (ROOT / "docs/openapi.yaml").read_text(encoding="utf-8"),
                (ROOT / "docs/asyncapi.yaml").read_text(encoding="utf-8"),
            ]
        )
        for state in DecisionState:
            self.assertIn(f"- {state.value}", contracts)

    def test_clickhouse_schema_contains_history_tables_and_ttls(self):
        schema = (ROOT / "migrations/clickhouse/001_initial.sql").read_text(
            encoding="utf-8"
        )
        for table in (
            "market_quote_snapshot",
            "market_bar_1m",
            "market_feature_realtime",
        ):
            self.assertIn(f"banxia.{table}", schema)
        self.assertIn("INTERVAL 90 DAY DELETE", schema)
        self.assertIn("INTERVAL 5 YEAR DELETE", schema)


class ComposeLayoutTest(unittest.TestCase):
    def test_compose_declares_required_local_services(self):
        path = ROOT / "deploy/compose/docker-compose.yml"
        compose = path.read_text(encoding="utf-8")
        services = yaml.safe_load(compose)["services"]
        for service in (
            "postgres",
            "clickhouse",
            "redis",
            "minio",
            "kafka",
            "flink-jobmanager",
            "flink-taskmanager",
            "flink-feature-job",
            "market-collector",
            "market-sink",
            "strategy-engine",
            "outbox-relay",
            "projection-worker",
            "report-worker",
            "report-scheduler",
            "api",
            "prometheus",
        ):
            self.assertIn(service, services)
        self.assertIn("../../migrations/postgres", compose)
        self.assertIn("../../migrations/clickhouse", compose)
        self.assertIn("RESTARTING", compose)
        self.assertIn("RECONCILING", compose)
        self.assertIn("16:30,23:30", compose)

    def test_flink_job_computes_sector_and_volume_features(self):
        sql = (ROOT / "deploy/flink/sql/realtime_features.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("market.quote.snapshot.v1", sql)
        self.assertIn("market.bar.1m.v1", sql)
        self.assertIn("market.feature.realtime.v1", sql)
        self.assertIn("sector_rise_ratio", sql)
        self.assertIn("baseline_volume", sql)
        self.assertIn("EXACTLY_ONCE", sql)
        self.assertIn(
            "'sink.transactional-id-prefix' = 'banxia-flink-sector-v1'",
            sql,
        )
        self.assertIn(
            "'sink.transactional-id-prefix' = 'banxia-flink-volume-v1'",
            sql,
        )

    def test_kubernetes_has_runtime_controls_and_daily_job(self):
        text = (ROOT / "deploy/kubernetes/base/platform.yaml").read_text(
            encoding="utf-8"
        )
        manifests = tuple(yaml.safe_load_all(text))
        kinds = {item["kind"] for item in manifests}
        self.assertIn("Deployment", kinds)
        self.assertIn("CronJob", kinds)
        self.assertIn("PodDisruptionBudget", kinds)
        self.assertIn("HorizontalPodAutoscaler", kinds)
        self.assertIn("NetworkPolicy", kinds)
        cron_jobs = {
            item["metadata"]["name"]: item["spec"]["schedule"]
            for item in manifests
            if item["kind"] == "CronJob"
        }
        self.assertEqual(cron_jobs["report-worker-1630"], "30 16 * * 1-5")
        self.assertEqual(cron_jobs["report-worker-2330"], "30 23 * * 1-5")
        self.assertIn("readinessProbe", text)
        self.assertIn("resources:", text)

    def test_topic_initializer_matches_event_contract(self):
        script = (ROOT / "deploy/compose/kafka/create-topics.sh").read_text(
            encoding="utf-8"
        )
        for topic in (
            "market.quote.snapshot.v1",
            "market.bar.1m.v1",
            "market.feature.realtime.v1",
            "strategy.plan.created.v1",
            "strategy.decision.v1",
            "strategy.audit.v1",
            "report.generated.v1",
        ):
            self.assertIn(topic, script)
        self.assertIn('"${topic}.dlq"', script)


class WebAssetTest(unittest.TestCase):
    def test_report_date_control_queries_trade_date_with_fixed_size(self):
        app = (ROOT / "src/banxia_strategy/web/app.js").read_text(
            encoding="utf-8"
        )
        html = (ROOT / "src/banxia_strategy/web/index.html").read_text(
            encoding="utf-8"
        )
        css = (ROOT / "src/banxia_strategy/web/styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("/api/v1/reports/${encodeURIComponent(asOf)}", app)
        self.assertIn("report.trade_date || report.as_of", app)
        self.assertIn('timeZone: "Asia/Shanghai"', app)
        self.assertIn('id="report-date" type="date"', html)
        self.assertIn("width: 148px", css)
        self.assertIn("flex-basis: 122px", css)
        self.assertIn("flex-wrap: nowrap", css)
        self.assertIn("white-space: nowrap", css)
        self.assertIn(".brand-mark,\n  .brand small {\n    display: none;", css)
        self.assertNotIn("<select id=\"report-date\"", html)


if __name__ == "__main__":
    unittest.main()
