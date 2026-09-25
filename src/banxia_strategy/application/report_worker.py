from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from ..application.persistence import PersistenceResult, persist_report_copy
from ..mootdx_provider import MootdxProvider
from ..storage_config import StorageSettings
from ..strategy import DailyReport, StrategyConfig, StrategyEngine, write_report
from ..strategy_config import stamp_report
from ..adapters.postgres import PostgresStorage, STRATEGY_CODE, _uuid
from ..adapters.strategy_catalog import CatalogConfigStore


class NonTradingDayError(RuntimeError):
    """Raised when a report is requested for a non-trading day."""


def write_completion_marker(
    report: DailyReport,
    paths: Mapping[str, Path],
    persistence: PersistenceResult,
) -> Path:
    if persistence.identity is None:
        raise RuntimeError("report persistence did not return a strategy run")
    completion_path = paths["json"].parent / ".report-complete.json"
    temporary_path = completion_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "as_of": report.as_of,
                "generated_at": report.generated_at,
                "run_id": persistence.identity.run_id,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(completion_path)
    return completion_path


class ReportWorker:
    """Generate, persist and announce one idempotent daily strategy report."""

    def __init__(
        self,
        *,
        strategy_config_path: Path,
        output_dir: Path,
        storage_settings: StorageSettings,
        provider: Optional[Any] = None,
        strategy_id: Optional[str] = None,
    ):
        self.strategy_config_path = strategy_config_path
        self.output_dir = output_dir
        self.storage_settings = storage_settings
        self.provider = provider or MootdxProvider()
        self.strategy_id = strategy_id

    def run(
        self,
        requested_date: Optional[date] = None,
    ) -> tuple[DailyReport, Mapping[str, Path], PersistenceResult]:
        # Both scheduled runs and queued page refreshes resolve the latest saved
        # config here, at execution time. The selected report date is data scope,
        # not a request to reuse that report's historical strategy snapshot.
        strategy = None
        default_id = _uuid(f"strategy:{STRATEGY_CODE}")
        resolved_strategy_id = self.strategy_id
        if self.storage_settings.enabled or self.strategy_id:
            repository = PostgresStorage(self.storage_settings.postgres_dsn)
            try:
                if not resolved_strategy_id:
                    active = repository.get_active_strategy()
                    if active is None:
                        raise ValueError("当前没有激活策略，报告任务已跳过")
                    if isinstance(active, Mapping):
                        resolved_strategy_id = active["strategy_id"]
                if resolved_strategy_id:
                    strategy = repository.get_strategy(resolved_strategy_id)
                    if not strategy or strategy["archived"]:
                        raise ValueError("策略不存在或已删除")
                    config = CatalogConfigStore(repository, resolved_strategy_id).read()
                else:
                    config = StrategyConfig.from_file(self.strategy_config_path)
            finally:
                repository.close()
        else:
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
        stamp_report(report, self.storage_settings)
        report.strategy_id = resolved_strategy_id or default_id
        report.strategy_code = strategy["code"] if strategy else STRATEGY_CODE
        report.strategy_name = strategy["name"] if strategy else "首板晋级二板策略"
        output = self.output_dir / "strategies" / resolved_strategy_id if strategy else self.output_dir
        paths = write_report(report, output)
        persistence = persist_report_copy(
            report.to_dict(),
            paths,
            strategy_config=asdict(config),
            settings=self.storage_settings,
            enqueue_events=True,
        )
        if self.storage_settings.enabled:
            repository = PostgresStorage(self.storage_settings.postgres_dsn)
            try:
                repository.save_trading_sessions(self.provider.trading_dates())
                self._save_actuals(repository, report)
                if report.next_session:
                    self._save_actuals(repository, report, report.next_session)
            finally:
                repository.close()
        write_completion_marker(report, paths, persistence)
        return report, paths, persistence

    def _save_actuals(self, repository, report, target_date=None):
        from ..research import label_candidate, summarize
        from ..intraday import plan_for

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        target_date = target_date or report.as_of
        if date.fromisoformat(target_date) > now.date() or (target_date == str(now.date()) and now.hour < 15):
            return
        day = repository.get_strategy_day(report.strategy_id, target_date)
        execution = day.get("execution_plan") if day else None
        if not execution:
            return
        snapshot = {"requested_end": target_date, "histories": getattr(self.provider, "_histories", {})}
        outcomes = [label_candidate({**candidate, "plan": plan_for(candidate)}, target_date, snapshot)
                    for candidate in execution.get("candidates", [])]
        repository.save_day_actuals(report.strategy_id, target_date, {
            "status": "complete" if all(item["status"] == "observed" for item in outcomes) else "missing",
            "outcomes": outcomes, "summary": summarize(outcomes),
            "stocks": day.get("actuals", {}).get("stocks", {}),
            "source": "mootdx", "kind": "market_validation",
        }, execution.get("plan_id"))
