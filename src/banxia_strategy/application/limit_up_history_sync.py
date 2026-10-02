from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from ..mootdx_provider import MootdxProvider


DEFAULT_LIMIT_UP_HISTORY_START = date(2025, 1, 1)
INCREMENTAL_LOOKBACK_DAYS = 14


class LimitUpHistorySync:
    """Backfill and incrementally refresh limit-up facts from mootdx."""

    def __init__(
        self,
        *,
        repository: Any,
        provider: Optional[MootdxProvider] = None,
    ) -> None:
        self.repository = repository
        self.provider = provider or MootdxProvider()

    def run(
        self,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> Mapping[str, Any]:
        with self.repository.limit_up_history_lock():
            return self._run(start_date=start_date, end_date=end_date)

    def _run(self, *, start_date: Optional[date], end_date: Optional[date]) -> Mapping[str, Any]:
        end = end_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        latest = self.repository.latest_limit_up_history_date()
        start = start_date or (
            max(
                DEFAULT_LIMIT_UP_HISTORY_START,
                latest - timedelta(days=INCREMENTAL_LOOKBACK_DAYS),
            )
            if latest is not None
            else DEFAULT_LIMIT_UP_HISTORY_START
        )
        if start > end:
            raise ValueError("start_date cannot be later than end_date")

        run_id = self.repository.begin_limit_up_history_sync(start, end)
        try:
            result = self.provider.limit_up_history(start, end)
            row_count = self.repository.complete_limit_up_history_sync(
                run_id,
                result["rows"],
                universe_count=result["universe_count"],
                history_count=result["history_count"],
                missing_symbols=result["missing_symbols"],
                successful_symbols=result["successful_symbols"],
                coverage=result["coverage"],
                effective_end=result["end_date"],
            )
            return {
                **result,
                "run_id": run_id,
                "row_count": row_count,
            }
        except Exception as exc:
            self.repository.fail_limit_up_history_sync(run_id, exc)
            raise
