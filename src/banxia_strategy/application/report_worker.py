from __future__ import annotations

from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

from ..application.persistence import PersistenceResult, persist_report_copy
from ..mootdx_provider import MootdxProvider
from ..storage_config import StorageSettings
from ..strategy import DailyReport, StrategyConfig, StrategyEngine, write_report


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
        report = StrategyEngine(self.provider, config).run(requested_date)
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
