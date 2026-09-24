from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from banxia_strategy.application.report_scheduler import (
    next_scheduled_at,
    parse_schedule,
    recent_scheduled_slots,
    report_is_fresh,
)
from banxia_strategy.application.report_worker import (
    NonTradingDayError,
    ReportWorker,
)
from banxia_strategy.config import RuntimeSettings
from banxia_strategy.ports.storage import ReportIdentity
from banxia_strategy.service import (
    _catch_up_report,
    _consume_report_refresh,
    _generate_scheduled_report,
)
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
        self.assertEqual(settings.report_schedule, ("16:30", "23:30"))

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
        schedule = parse_schedule(("16:30", "23:30"))
        timezone = ZoneInfo("Asia/Shanghai")

        afternoon = datetime(2026, 9, 24, 16, 31, tzinfo=timezone)
        late = datetime(2026, 9, 24, 23, 31, tzinfo=timezone)

        self.assertEqual(
            next_scheduled_at(afternoon, schedule),
            datetime(2026, 9, 24, 23, 30, tzinfo=timezone),
        )
        self.assertEqual(
            next_scheduled_at(late, schedule),
            datetime(2026, 9, 25, 16, 30, tzinfo=timezone),
        )

    def test_recent_schedule_finds_previous_day_after_midnight(self):
        schedule = parse_schedule(("16:30", "23:30"))
        timezone = ZoneInfo("Asia/Shanghai")
        slots = recent_scheduled_slots(
            datetime(2026, 9, 25, 0, 3, tzinfo=timezone),
            schedule,
            lookback_days=2,
        )

        self.assertEqual(
            slots,
            (
                datetime(2026, 9, 24, 23, 30, tzinfo=timezone),
                datetime(2026, 9, 23, 23, 30, tzinfo=timezone),
            ),
        )

    def test_report_must_be_generated_after_the_due_slot(self):
        timezone = ZoneInfo("Asia/Shanghai")
        scheduled_at = datetime(2026, 9, 24, 23, 30, tzinfo=timezone)
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            report_dir = output_dir / "2026-09-24"
            report_dir.mkdir()
            report_path = report_dir / "candidates.json"
            report_path.write_text(
                '{"as_of":"2026-09-24",'
                '"generated_at":"2026-09-24T16:31:00+08:00"}',
                encoding="utf-8",
            )
            completion_path = report_dir / ".report-complete.json"
            completion_path.write_text(
                '{"as_of":"2026-09-24",'
                '"generated_at":"2026-09-24T16:31:00+08:00",'
                '"run_id":"run-1"}',
                encoding="utf-8",
            )
            self.assertFalse(report_is_fresh(output_dir, scheduled_at))

            report_path.write_text(
                '{"as_of":"2026-09-24",'
                '"generated_at":"2026-09-24T23:31:00+08:00"}',
                encoding="utf-8",
            )
            self.assertFalse(report_is_fresh(output_dir, scheduled_at))
            completion_path.write_text(
                '{"as_of":"2026-09-24",'
                '"generated_at":"2026-09-24T23:31:00+08:00",'
                '"run_id":"run-2"}',
                encoding="utf-8",
            )
            self.assertTrue(report_is_fresh(output_dir, scheduled_at))

    def test_startup_recovers_latest_missing_schedule(self):
        timezone = ZoneInfo("Asia/Shanghai")
        settings = RuntimeSettings(report_output_dir=Path("reports"))
        logger = Mock()
        stop = Mock()
        schedule = parse_schedule(("16:30", "23:30"))
        now = datetime(2026, 9, 25, 0, 3, tzinfo=timezone)

        with patch(
            "banxia_strategy.service.report_is_fresh",
            return_value=False,
        ), patch(
            "banxia_strategy.service._generate_report",
            return_value=True,
        ) as generate:
            _catch_up_report(settings, logger, schedule, now, stop)

        generate.assert_called_once_with(settings, logger, date(2026, 9, 24))

    def test_scheduled_report_retries_a_transient_failure(self):
        timezone = ZoneInfo("Asia/Shanghai")
        settings = RuntimeSettings(report_output_dir=Path("reports"))
        logger = Mock()
        stop = Mock()
        stop.wait.return_value = False
        scheduled_at = datetime(2026, 9, 24, 23, 30, tzinfo=timezone)

        with patch(
            "banxia_strategy.service._generate_report",
            side_effect=[RuntimeError("temporary"), True],
        ) as generate:
            result = _generate_scheduled_report(
                settings,
                logger,
                scheduled_at.date(),
                scheduled_at,
                stop,
                retry_delays=(0.01,),
            )

        self.assertTrue(result)
        self.assertEqual(generate.call_count, 2)
        stop.wait.assert_called_once_with(0.01)

    def test_queued_refresh_uses_the_scheduled_report_transaction(self):
        repository = Mock()
        repository.claim_report_refresh.return_value = {
            "job_id": "job-1",
            "payload": {"trade_date": "2026-09-24"},
        }
        settings = RuntimeSettings(report_output_dir=Path("reports"))
        logger = Mock()
        stop = Mock()

        with patch(
            "banxia_strategy.service._generate_scheduled_report",
            return_value=True,
        ) as generate:
            consumed = _consume_report_refresh(
                repository,
                settings,
                logger,
                stop,
            )

        self.assertTrue(consumed)
        generate.assert_called_once()
        self.assertEqual(generate.call_args.args[2], date(2026, 9, 24))
        repository.finish_report_refresh.assert_called_once()
        finish = repository.finish_report_refresh.call_args
        self.assertEqual(finish.args, ("job-1",))
        self.assertTrue(finish.kwargs["succeeded"])
        self.assertEqual(
            finish.kwargs["result"]["trade_date"],
            "2026-09-24",
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
                completion = json.loads(
                    (Path(directory) / ".report-complete.json").read_text(
                        encoding="utf-8"
                    )
                )

        self.assertIs(result, report)
        self.assertEqual(persistence.identity, identity)
        self.assertEqual(completion["run_id"], "run")
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
            generated_at="2026-09-24T16:30:00+08:00",
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
