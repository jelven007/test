"""Idempotently backfill strategy-day plans and historical market outcomes."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapters.postgres import PostgresStorage
from .research import evaluate_config
from .research_data import ResearchCollector
from .storage_config import StorageSettings
from .strategy_config import StrategyConfig


def _references(snapshot, start: date, end: date):
    sessions = sorted(date.fromisoformat(value) for value in snapshot["calendar"])
    in_scope = [value for value in sessions if start <= value <= end]
    if not in_scope:
        raise ValueError("指定范围内没有交易日")
    previous = [value for value in sessions if value < in_scope[0]]
    return ([previous[-1]] if previous else []) + in_scope


def backfill_strategy(repository, strategy, snapshot, start: date, end: date, *, commit: str):
    references = _references(snapshot, start, end)
    config = StrategyConfig.from_mapping(strategy.get("config") or {})
    result = evaluate_config(
        snapshot, [value.isoformat() for value in references], config, commit=commit,
    )
    counters = {"plans": 0, "actuals": 0, "preserved_plans": 0, "preserved_actuals": 0}
    for day in result["days"]:
        reference = date.fromisoformat(day["reference_date"])
        plan_date = date.fromisoformat(day["plan_date"]) if day["plan_date"] else None
        report = {
            **day["report"],
            "strategy_id": strategy["strategy_id"],
            "strategy_code": strategy["code"],
            "strategy_name": strategy["name"],
            "catalog_backfill": {
                "source": "mootdx",
                "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "scope_start": start.isoformat(),
                "scope_end": end.isoformat(),
            },
        }
        existing = repository.get_strategy_day(strategy["strategy_id"], day["reference_date"])
        should_save_plan = (
            reference >= start
            and (not existing or not existing["next_plan"])
        )
        execution = (
            repository.get_strategy_day(strategy["strategy_id"], day["plan_date"])
            if plan_date and start <= plan_date <= end else None
        )
        should_fill_execution = (
            plan_date is not None
            and start <= plan_date <= end
            and (not execution or not execution["execution_plan"])
        )
        if should_save_plan or should_fill_execution:
            repository.fill_daily_report_gaps(strategy["strategy_id"], report)
        if should_save_plan:
            counters["plans"] += 1
        elif reference >= start:
            counters["preserved_plans"] += 1

        if not plan_date or not start <= plan_date <= end:
            continue
        execution = repository.get_strategy_day(strategy["strategy_id"], day["plan_date"])
        if not execution or not execution["execution_plan"]:
            raise RuntimeError(f"{strategy['name']} {day['plan_date']} 缺少执行计划")
        if execution["actuals"]:
            counters["preserved_actuals"] += 1
            continue
        summary = day["summary"]
        status = (
            "pending" if summary["pending_count"] else
            "missing" if summary["missing_count"] else
            "complete"
        )
        repository.save_day_actuals(
            strategy["strategy_id"],
            day["plan_date"],
            {
                "status": status,
                "outcomes": day["outcomes"],
                "summary": summary,
                "kind": "historical_market_validation",
                "source": "mootdx",
            },
            execution["execution_plan"].get("plan_id"),
        )
        counters["actuals"] += 1
    return {**counters, "summary": result["summary"]}


def backfill_catalog(repository, snapshot, start: date, end: date, *, commit="backfill"):
    if start > end:
        raise ValueError("start must be on or before end")
    sessions = [
        value for value in snapshot["calendar"]
        if start.isoformat() <= value <= end.isoformat()
    ]
    repository.save_trading_sessions(sessions)
    results = {}
    for strategy in repository.list_strategies():
        results[strategy["strategy_id"]] = {
            "name": strategy["name"],
            **backfill_strategy(repository, strategy, snapshot, start, end, commit=commit),
        }
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "session_count": len(sessions),
        "strategies": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="补齐策略目录中的历史次日计划与当日行情验证")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    parser.add_argument("--start", type=date.fromisoformat, default=today - timedelta(days=365))
    parser.add_argument("--end", type=date.fromisoformat, default=today - timedelta(days=1))
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--output", type=Path, default=Path("research/catalog-backfill"))
    parser.add_argument("--history-sessions", type=int, default=320)
    args = parser.parse_args(argv)

    settings = StorageSettings.from_env()
    snapshot_path = args.snapshot or args.output / f"{args.start:%Y%m%d}-{args.end:%Y%m%d}" / "snapshot.json"
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text())
    else:
        # Include enough preceding sessions to create the first in-range day's execution plan.
        collection_start = args.start - timedelta(days=14)
        snapshot = ResearchCollector(history_sessions=args.history_sessions).collect(
            collection_start, args.end, snapshot_path.parent,
        )
    if snapshot["requested_end"] < args.end.isoformat():
        raise ValueError("快照未覆盖回填结束日期")

    repository = PostgresStorage(settings.postgres_dsn)
    try:
        result = backfill_catalog(
            repository, snapshot, args.start, args.end, commit=settings.code_commit,
        )
    finally:
        repository.close()
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
