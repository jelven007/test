from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from banxia_strategy.application.report_scheduler import (
    next_scheduled_at,
    parse_schedule,
)
from banxia_strategy.application.report_worker import (
    NonTradingDayError,
    ReportWorker,
)
from banxia_strategy.config import RuntimeSettings
from banxia_strategy.ports.storage import ReportIdentity
from banxia_strategy.storage_config import StorageSettings
from banxia_strategy.strategy import DailyReport


class CalendarProvider:
    def __init__(self, *sessions: date):
        self.sessions = sessions

    def trading_dates(self):
        return self.sessions


class RuntimeSettingsTest(unittest.TestCase):
    def test_service_settings_are_loaded_from_environment(self):
        settings = RuntimeSettings.from_env(
            {
                "BANXIA_REPORT_DIRS": "one,two",
                "BANXIA_REPORT_OUTPUT_DIR": "generated",
                "BANXIA_REPORT_DATE": "2026-09-24",
                "BANXIA_REPORT_SCHEDULE": "15:55,23:25",
                "BANXIA_METRICS_PORT": "9200",
                "BANXIA_STORAGE_MODE": "required",
            },
            service_name="report-worker",
        )
        self.assertEqual(settings.report_dirs, (Path("one"), Path("two")))
        self.assertEqual(settings.report_output_dir, Path("generated"))
        self.assertEqual(settings.report_date, "2026-09-24")
        self.assertEqual(settings.report_schedule, ("15:55", "23:25"))
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

    def test_default_report_schedule_has_two_daily_updates(self):
        settings = RuntimeSettings.from_env({"BANXIA_REPORT_DIRS": "reports"})
        self.assertEqual(settings.report_schedule, ("16:00", "23:30"))

    def test_invalid_report_schedule_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "BANXIA_REPORT_SCHEDULE"):
            RuntimeSettings.from_env(
                {
                    "BANXIA_REPORT_DIRS": "reports",
                    "BANXIA_REPORT_SCHEDULE": "24:00",
                }
            )


class ReportSchedulerTest(unittest.TestCase):
    def test_next_schedule_uses_shanghai_time_and_rolls_to_next_day(self):
        schedule = parse_schedule(("16:00", "23:30"))
        timezone = ZoneInfo("Asia/Shanghai")

        afternoon = datetime(2026, 9, 24, 16, 1, tzinfo=timezone)
        late = datetime(2026, 9, 24, 23, 31, tzinfo=timezone)

        self.assertEqual(
            next_scheduled_at(afternoon, schedule),
            datetime(2026, 9, 24, 23, 30, tzinfo=timezone),
        )
        self.assertEqual(
            next_scheduled_at(late, schedule),
            datetime(2026, 9, 25, 16, 0, tzinfo=timezone),
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
                provider=CalendarProvider(date(2026, 9, 23)),
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

    def test_non_trading_day_does_not_generate_or_persist_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "strategy.json"
            config.write_text("{}", encoding="utf-8")
            worker = ReportWorker(
                strategy_config_path=config,
                output_dir=Path(directory),
                storage_settings=StorageSettings(mode="off"),
                provider=CalendarProvider(date(2026, 9, 23)),
            )
            with patch(
                "banxia_strategy.application.report_worker.StrategyEngine.run"
            ) as run:
                with self.assertRaisesRegex(NonTradingDayError, "not a trading day"):
                    worker.run(date(2026, 9, 24))
            run.assert_not_called()

    def test_report_cannot_fall_back_to_an_earlier_trading_day(self):
        report = DailyReport(
            as_of="2026-09-23",
            next_session="2026-09-25",
            generated_at="2026-09-24T16:00:00+08:00",
            data_source="mootdx",
            data_sessions=["2026-09-23"],
            market={"regime": "中性", "score": 60},
            candidates=[],
            rejected_count=0,
            disclaimer="research only",
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "strategy.json"
            config.write_text("{}", encoding="utf-8")
            worker = ReportWorker(
                strategy_config_path=config,
                output_dir=Path(directory),
                storage_settings=StorageSettings(mode="off"),
                provider=CalendarProvider(date(2026, 9, 24)),
            )
            with patch(
                "banxia_strategy.application.report_worker.StrategyEngine.run",
                return_value=report,
            ), self.assertRaisesRegex(RuntimeError, "requested 2026-09-24"):
                worker.run(date(2026, 9, 24))


if __name__ == "__main__":
    unittest.main()
