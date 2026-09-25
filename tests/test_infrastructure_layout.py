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

    def test_strategy_name_is_mutable_but_config_and_lineage_are_not(self):
        migration = (
            ROOT / "migrations/postgres/007_mutable_strategy_name.sql"
        ).read_text(encoding="utf-8")
        self.assertNotIn("NEW.name IS DISTINCT FROM OLD.name", migration)
        self.assertIn(
            "NEW.current_config IS DISTINCT FROM OLD.current_config",
            migration,
        )
        self.assertIn(
            "NEW.parent_strategy_id IS DISTINCT FROM OLD.parent_strategy_id",
            migration,
        )

    def test_strategy_delete_migration_allows_only_parent_detachment(self):
        migration = (
            ROOT / "migrations/postgres/008_strategy_cascade_delete.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("OLD.parent_strategy_id IS NOT NULL", migration)
        self.assertIn("NEW.parent_strategy_id IS NULL", migration)
        self.assertIn(
            "NEW.config_changes IS DISTINCT FROM OLD.config_changes",
            migration,
        )

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
        for service in ("report-worker", "report-scheduler"):
            self.assertEqual(services[service]["network_mode"], "host")
            self.assertIn(
                "127.0.0.1",
                services[service]["environment"]["BANXIA_POSTGRES_DSN"],
            )
            self.assertIn(
                "127.0.0.1",
                services[service]["environment"]["BANXIA_MINIO_ENDPOINT"],
            )
        self.assertEqual(
            services["report-worker"]["environment"]["BANXIA_METRICS_PORT"],
            "${BANXIA_REPORT_WORKER_METRICS_PORT:-9101}",
        )

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
    def test_all_pages_use_shared_ui_standards(self):
        web = ROOT / "src/banxia_strategy/web"
        pages = [
            web / "index.html",
            web / "monitor.html",
            web / "strategy.html",
            web / "research.html",
        ]
        for page in pages:
            html = page.read_text(encoding="utf-8")
            self.assertIn("/ui-standard.css?v=20260925.5", html, page.name)
            self.assertIn('class="primary-nav"', html, page.name)
            self.assertRegex(html, r'<main[^>]*class="[^"]*page-main')

        for page in pages[1:]:
            html = page.read_text(encoding="utf-8")
            self.assertIn("page-header", html, page.name)
        for page in (web / "monitor.html", web / "strategy.html", web / "research.html"):
            html = page.read_text(encoding="utf-8")
            for table in html.split("<table")[1:]:
                self.assertIn("data-table", table.split(">", 1)[0], page.name)

        standard = (web / "ui-standard.css").read_text(encoding="utf-8")
        for selector in (
            ".ui-control",
            ".ui-button",
            ".status-badge",
            ".ui-status",
            ".segmented-control",
            ".data-table",
            ".notice",
        ):
            self.assertIn(selector, standard)
        self.assertIn("@media (max-width: 560px)", standard)
        self.assertIn("overflow-x: clip", standard)
        self.assertIn(
            ".settings-fields {\n    grid-template-columns: minmax(0, 1fr);",
            standard,
        )
        self.assertNotIn(
            ".dashboard-topbar #page-strategy.ui-control {\n"
            "    grid-column: 1 / -1;",
            standard,
        )
        self.assertNotRegex(standard, r"letter-spacing:\s*-")
        self.assertNotIn(".watch-table thead { display: none;", (web / "monitor.css").read_text(encoding="utf-8"))

        strategy_html = (web / "strategy.html").read_text(encoding="utf-8")
        research_html = (web / "research.html").read_text(encoding="utf-8")
        self.assertIn('class="strategy-table-wrap" tabindex="0" role="region"', strategy_html)
        self.assertIn('id="settings-message" class="ui-status"', strategy_html)
        self.assertIn('id="research-status" class="ui-status"', research_html)
        self.assertEqual(research_html.count('class="research-table-scroll" tabindex="0" role="region"'), 4)

    def test_report_date_control_queries_trade_date_with_fixed_size(self):
        app = (ROOT / "src/banxia_strategy/web/app.js").read_text(
            encoding="utf-8"
        )
        html = (ROOT / "src/banxia_strategy/web/index.html").read_text(
            encoding="utf-8"
        )
        monitor_html = (ROOT / "src/banxia_strategy/web/monitor.html").read_text(
            encoding="utf-8"
        )
        monitor_app = (ROOT / "src/banxia_strategy/web/monitor.js").read_text(
            encoding="utf-8"
        )
        css = (ROOT / "src/banxia_strategy/web/styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("/api/v1/reports/${encodeURIComponent(asOf)}", app)
        self.assertIn(
            "/api/v1/reports/${encodeURIComponent(asOf)}/refresh",
            app,
        )
        self.assertIn("/api/v1/report-jobs/${encodeURIComponent(job.job_id)}", app)
        self.assertIn("report.trade_date || report.as_of", app)
        self.assertIn('timeZone: "Asia/Shanghai"', app)
        self.assertIn('id="report-date" type="date"', html)
        self.assertRegex(html, r'<button id="refresh-button" type="button"[^>]*>刷新</button>')
        self.assertNotIn("复盘日期", html)
        self.assertIn('class="topbar dashboard-topbar"', monitor_html)
        self.assertIn('id="report-date" type="date"', monitor_html)
        self.assertRegex(
            monitor_html,
            r'<button id="refresh-button" type="button"[^>]*>刷新</button>',
        )
        self.assertIn('sync({ userInitiated: true })', monitor_app)
        self.assertIn('params.set("trade_date", reportDateInput.value)', monitor_app)
        self.assertNotIn("刷新数据", html)
        self.assertIn("width: 148px", css)
        self.assertIn("flex-basis: 122px", css)
        self.assertIn("flex-wrap: nowrap", css)
        self.assertIn("white-space: nowrap", css)
        self.assertIn(".brand-mark {\n    display: none;", css)
        self.assertNotIn("<select id=\"report-date\"", html)

    def test_all_pages_use_compact_bx_brand(self):
        web = ROOT / "src/banxia_strategy/web"
        for name in ("index.html", "monitor.html", "research.html", "strategy.html"):
            html = (web / name).read_text(encoding="utf-8")
            self.assertIn(
                '<span class="brand-mark" aria-hidden="true">BX</span>',
                html,
                name,
            )
            self.assertNotIn("策略观察工作台", html, name)
            self.assertNotIn("<small>策略观察工作台</small>", html, name)
        standard = (web / "ui-standard.css").read_text(encoding="utf-8")
        self.assertIn(
            "grid-template-columns: auto 112px auto 132px 52px;",
            standard,
        )
        self.assertIn("grid-template-columns: 92px 122px 48px;", standard)
        self.assertIn("grid-template-columns: 78px 114px 42px;", standard)

    def test_strategy_page_defaults_to_list_and_links_to_detail(self):
        web = ROOT / "src/banxia_strategy/web"
        html = (web / "strategy.html").read_text(encoding="utf-8")
        script = (web / "strategy.js").read_text(encoding="utf-8")
        css = (web / "strategy.css").read_text(encoding="utf-8")
        self.assertIn('id="strategy-list-view"', html)
        self.assertIn('id="strategy-detail-view" hidden', html)
        self.assertIn('id="strategy-name-form"', html)
        self.assertIn('id="save-strategy-dialog"', html)
        self.assertIn('id="save-strategy-action"', html)
        self.assertNotIn('id="save-strategy-name"', html)
        self.assertNotIn('id="copy-strategy"', html)
        self.assertNotIn('id="save-button"', html)
        self.assertIn("/strategy?strategy_id=${encodeURIComponent(item.strategy_id)}", script)
        self.assertIn("JSON.stringify({name})", script)
        self.assertIn("parent_strategy_id: strategyId", script)
        self.assertIn("参数变化不会覆盖初始策略", script)
        self.assertIn('save.textContent = saveAs ? "另存" : "保存"', script)
        self.assertIn('item?.enabled ? "停用" : "激活"', script)
        self.assertIn('item?.enabled ? "激活" : "停用"', script)
        self.assertNotIn("取消激活", script)
        self.assertIn('id="strategy-status" class="status-badge">停用</span>', html)
        self.assertIn('id="strategy-query-form"', html)
        self.assertIn('id="strategy-history-range"', html)
        self.assertIn("history_range:", script)
        self.assertIn("只有初始策略支持修改参数并另存", script)
        self.assertNotIn("const history = result.history_generation", script)
        self.assertRegex(
            css,
            r"#strategy-list-view \.strategy-list-heading \{[^}]*flex-direction: row;",
        )
        self.assertRegex(
            css,
            r"\.settings-main \.catalog-toolbar \{[^}]*flex-wrap: nowrap;",
        )

    def test_strategy_selector_keeps_management_navigation_on_list_view(self):
        web = ROOT / "src/banxia_strategy/web"
        script = (web / "strategy-selector.js").read_text(encoding="utf-8")
        self.assertIn('const scopedNavPaths = new Set(["/", "/monitor"]);', script)
        self.assertIn("if (!scopedNavPaths.has(link.pathname)) return;", script)
        for page in ("index.html", "monitor.html"):
            html = (web / page).read_text(encoding="utf-8")
            self.assertIn("/strategy-selector.js?v=20260925.2", html)


if __name__ == "__main__":
    unittest.main()
