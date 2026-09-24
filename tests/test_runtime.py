from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from banxia_strategy.application.report_worker import ReportWorker
from banxia_strategy.config import RuntimeSettings
from banxia_strategy.ports.storage import ReportIdentity
from banxia_strategy.storage_config import StorageSettings
from banxia_strategy.strategy import DailyReport


class RuntimeSettingsTest(unittest.TestCase):
    def test_service_settings_are_loaded_from_environment(self):
        settings = RuntimeSettings.from_env(
            {
                "BANXIA_REPORT_DIRS": "one,two",
                "BANXIA_REPORT_OUTPUT_DIR": "generated",
                "BANXIA_REPORT_DATE": "2026-09-24",
                "BANXIA_METRICS_PORT": "9200",
                "BANXIA_STORAGE_MODE": "required",
            },
            service_name="report-worker",
        )
        self.assertEqual(settings.report_dirs, (Path("one"), Path("two")))
        self.assertEqual(settings.report_output_dir, Path("generated"))
        self.assertEqual(settings.report_date, "2026-09-24")
        self.assertEqual(settings.metrics_port, 9200)
        self.assertEqual(settings.service_name, "report-worker")

    def test_invalid_metrics_port_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "BANXIA_METRICS_PORT"):
            RuntimeSettings.from_env(
                {
                    "BANXIA_REPORT_DIRS": "reports",
                    "BANXIA_METRICS_PORT": "0",
                }
            )


class ReportWorkerTest(unittest.TestCase):
    def test_report_is_persisted_with_outbox_events(self):
        report = DailyReport(
            as_of="2026-09-23",
            next_session="2026-09-24",
            generated_at="2026-09-23T16:20:00+08:00",
            data_source="mootdx",
            data_sessions=["2026-09-23"],
            market={"regime": "中性", "score": 60},
            candidates=[],
            rejected_count=0,
            disclaimer="research only",
        )
        identity = ReportIdentity("run", "version", "plan")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "strategy.json"
            config.write_text("{}", encoding="utf-8")
            worker = ReportWorker(
                strategy_config_path=config,
                output_dir=Path(directory),
                storage_settings=StorageSettings(mode="required"),
                provider=object(),
            )
            with patch(
                "banxia_strategy.application.report_worker.StrategyEngine.run",
                return_value=report,
            ), patch(
                "banxia_strategy.application.report_worker.write_report",
                return_value={"json": Path(directory) / "candidates.json"},
            ), patch(
                "banxia_strategy.application.report_worker.persist_report_copy"
            ) as persist:
                persist.return_value.identity = identity
                result, _paths, persistence = worker.run(
                    date.fromisoformat("2026-09-23")
                )

        self.assertIs(result, report)
        self.assertEqual(persistence.identity, identity)
        self.assertTrue(persist.call_args.kwargs["enqueue_events"])


if __name__ == "__main__":
    unittest.main()
