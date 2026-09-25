"""Materialize historical plans and intraday outcomes for one strategy."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .catalog_backfill import backfill_strategy
from .research_data import ResearchCollector


HISTORY_RANGE_DAYS = {
    "1d": 1,
    "1w": 7,
    "1m": 31,
    "1y": 365,
}


def history_window(history_range: str, *, today: Optional[date] = None):
    if history_range not in HISTORY_RANGE_DAYS:
        raise ValueError("历史范围必须是 1d、1w、1m 或 1y")
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=HISTORY_RANGE_DAYS[history_range] - 1)
    return start, end


def materialize_strategy_history(
    repository,
    strategy,
    *,
    history_range: str,
    start: date,
    end: date,
    output_root: Path,
    commit: str,
    collector_factory=ResearchCollector,
):
    if history_range not in HISTORY_RANGE_DAYS:
        raise ValueError("历史范围必须是 1d、1w、1m 或 1y")
    if start > end:
        raise ValueError("历史开始日期不能晚于结束日期")

    collection_start = start - timedelta(days=14)
    snapshot_dir = output_root / "strategy-history" / f"{collection_start:%Y%m%d}-{end:%Y%m%d}"
    snapshot = collector_factory(history_sessions=320).collect(
        collection_start,
        end,
        snapshot_dir,
    )
    sessions = sorted(
        date.fromisoformat(value)
        for value in snapshot["calendar"]
        if date.fromisoformat(value) <= end
    )
    in_scope = [value for value in sessions if value >= start]
    if not in_scope and sessions:
        in_scope = [sessions[-1]]
    if not in_scope:
        raise ValueError("指定范围内没有可用交易日")

    effective_start = in_scope[0]
    effective_end = in_scope[-1]
    repository.save_trading_sessions(
        value.isoformat()
        for value in sessions
        if effective_start <= value <= effective_end
    )
    result = backfill_strategy(
        repository,
        strategy,
        snapshot,
        effective_start,
        effective_end,
        commit=commit,
        replace_existing=True,
    )
    return {
        "strategy_id": strategy["strategy_id"],
        "history_range": history_range,
        "start": effective_start.isoformat(),
        "end": effective_end.isoformat(),
        "datasets": ["next_plan", "intraday_monitor"],
        **result,
    }
