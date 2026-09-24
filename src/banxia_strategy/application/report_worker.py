from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from ..application.persistence import PersistenceResult, persist_report_copy
from ..mootdx_provider import MootdxProvider
from ..storage_config import StorageSettings
from ..strategy import DailyReport, StrategyConfig, StrategyEngine, write_report


class NonTradingDayError(RuntimeError):
    """Raised when a report is requested for a non-trading day."""


class ReportWorker:
    """Generate, persist and announce one idempotent daily strategy report."""

    def __init__(
        self,
        *,
        strategy_config_path: Path,
        output_dir: Path,
        storage_settings: StorageSettings,
        provider: Optional[Any] = None,
    ):
        self.strategy_config_path = strategy_config_path
        self.output_dir = output_dir
        self.storage_settings = storage_settings
        self.provider = provider or MootdxProvider()

    def run(
        self,
        requested_date: Optional[date] = None,
    ) -> tuple[DailyReport, Mapping[str, Path], PersistenceResult]:
        config = StrategyConfig.from_file(self.strategy_config_path)
        target_date = requested_date or datetime.now(
            ZoneInfo(config.report_timezone)
        ).date()
        if target_date not in set(self.provider.trading_dates()):
            raise NonTradingDayError(
                f"{target_date.isoformat()} is not a trading day; report skipped"
            )

        report = StrategyEngine(self.provider, config).run(target_date)
        if report.as_of != target_date.isoformat():
            raise RuntimeError(
                "market data for the requested trading day is unavailable: "
                f"requested {target_date.isoformat()}, received {report.as_of}"
            )
        paths = write_report(report, self.output_dir)
        persistence = persist_report_copy(
            report.to_dict(),
            paths,
            strategy_config=asdict(config),
            settings=self.storage_settings,
            enqueue_events=True,
        )
        if persistence.identity is None:
            raise RuntimeError("report persistence did not return a strategy run")
        return report, paths, persistence
